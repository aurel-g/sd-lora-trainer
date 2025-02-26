import os
import torch
import numpy as np
from tqdm import tqdm
import pandas as pd
import PIL
from PIL import Image
from torch.utils.data import Dataset
from typing import Tuple, Dict, List

def prepare_image(
    pil_image: PIL.Image.Image, w: int = 512, h: int = 512, pipe=None,
) -> torch.Tensor:
    pil_image = pil_image.resize((w, h), resample=Image.BICUBIC, reducing_gap=1)
    image = pipe.image_processor.preprocess(pil_image)
    return image


def prepare_mask(
    pil_image: PIL.Image.Image, w: int = 512, h: int = 512
) -> torch.Tensor:
    pil_image = pil_image.resize((w, h), resample=Image.BICUBIC, reducing_gap=1)
    arr = np.array(pil_image.convert("L"))
    arr = arr.astype(np.float32) / 255.0
    arr = np.expand_dims(arr, 0)
    image = torch.from_numpy(arr).unsqueeze(0)
    return image


class PreprocessedDataset(Dataset):
    def __init__(
        self,
        data_dir: str,
        pipe,
        vae_encoder,
        size: List[int] = [512, 512],
        text_dropout: float = 0.0,
        aspect_ratio_bucketing: bool = False,
        train_batch_size: int = None, # required for aspect_ratio_bucketing
        substitute_caption_map: Dict[str, str] = {},
    ):
        super().__init__()
        self.data_dir = data_dir
        self.csv_path = os.path.join(data_dir, "captions.csv")
        self.data = pd.read_csv(self.csv_path, dtype={"caption": str})
        
        self.captions = self.data["caption"]
        self.captions = self.captions.str.lower()
        for key, value in substitute_caption_map.items():
            self.captions = self.captions.str.replace(key.lower(), value)

        self.captions = self.captions.fillna("")
        self.image_path = self.data["image_path"]

        if "mask_path" not in self.data.columns:
            self.mask_path = None
        else:
            self.mask_path = self.data["mask_path"]

        self.pipe = pipe
        self.vae_encoder = vae_encoder
        self.vae_scaling_factor = self.vae_encoder.config.scaling_factor
        self.text_dropout = text_dropout
        self.size = size

        # If the training data is small we can keep everything in memory, otherwise offload to disk
        self.do_cache = True if len(self.data) < 500 else False

        if self.do_cache:
            print("Encoding latents, masks and captions and storing in memory...\n")
            self.vae_latents = []
            self.masks = []

            for idx in tqdm(range(len(self.data))):
                vae_latent, mask, _ = self._process(idx)
                self.vae_latents.append(vae_latent)
                self.masks.append(mask.detach())

        else: # Store the latents and masks on disk
            print("Encoding latents, masks and captions and storing on disk...\n")
            self.vae_latents = None
            self.masks = None

            for idx in tqdm(range(len(self.data))):
                vae_latent, mask, image_path = self._process(idx)
                torch.save(vae_latent, os.path.join(self.data_dir, f"{idx}_vae_latent.pt"))
                torch.save(mask, os.path.join(self.data_dir, f"{idx}_mask.pt"))

        del self.vae_encoder
        torch.cuda.empty_cache()

        if aspect_ratio_bucketing:
            print("Using aspect ratio bucketing.")
            assert train_batch_size is not None, f"Please also provide a `train_batch_size` when you have set `aspect_ratio_bucketing == True`"
            from .utils.aspect_ratio_bucketing import BucketManager
            aspect_ratios = {}
            for idx in range(len(self.data)):
                aspect_ratios[idx] = Image.open(os.path.join(self.data_dir, self.image_path[idx])).size

            self.bucket_manager = BucketManager(
                aspect_ratios = aspect_ratios,
                bsz = train_batch_size,
                debug=True
            )
        else:
            print("Not using aspect ratio bucketing.")
            self.bucket_manager = None

    def get_aspect_ratio_bucketed_batch(self):
        assert self.bucket_manager is not None, f"Expected self.bucket_manager to not be None! In order to get an aspect ratio bucketed batch, please set aspect_ratio_bucketing = True and set a value for train_batch_size when doing __init__()"
        indices, resolution = self.bucket_manager.get_batch()
        tok1, tok2, vae_latents, masks = [], [], [], []
        
        for idx in indices:
            if  self.tokenizer_2 is None:
                t1, v, m = self.__getitem__(idx = idx, bucketing_resolution=resolution)
            else:
                (t1, t2), v, m = self.__getitem__(idx = idx, bucketing_resolution=resolution)
                tok2.append(t2.unsqueeze(0))

            tok1.append(t1.unsqueeze(0))
            vae_latents.append(v.unsqueeze(0))
            masks.append(m.unsqueeze(0))

        tok1 = torch.cat(tok1, dim = 0)
        if  self.tokenizer_2 is None:
            pass
        else:
            tok2 = torch.cat(tok2, dim = 0)
        vae_latents = torch.cat(vae_latents, dim = 0)
        masks = torch.cat(masks, dim = 0)

        if self.tokenizer_2 is None:
            return (tok1, None), vae_latents, masks
        else:
            return (tok1, tok2), vae_latents, masks

    def __len__(self) -> int:
        return len(self.data)

    @torch.no_grad()
    def _process(
        self, idx: int, bucketing_resolution: tuple = None
    ) -> Tuple[Tuple[torch.Tensor, torch.Tensor], torch.Tensor, torch.Tensor]:
        image_path = self.image_path[idx]
        image_path = os.path.join(self.data_dir, image_path)
        image = PIL.Image.open(image_path).convert("RGB")
        if bucketing_resolution is None:
            image = prepare_image(image, w = self.size[0], h = self.size[1], pipe = self.pipe).to(
                dtype=self.vae_encoder.dtype
            )
        else:
            image = prepare_image(image, w = bucketing_resolution[0], h = bucketing_resolution[1], pipe = self.pipe).to(
                dtype=self.vae_encoder.dtype
            )

        vae_latent = self.vae_encoder.encode(image.to(self.vae_encoder.device)).latent_dist
        dummy_vae_latent = vae_latent.sample()

        if self.mask_path is None:
            mask = torch.ones_like(dummy_vae_latent, dtype=self.vae_encoder.dtype)

        else:
            mask_path = self.mask_path[idx]
            mask_path = os.path.join(self.data_dir, mask_path)
            mask = PIL.Image.open(mask_path)
            mask = prepare_mask(mask, self.size[0], self.size[1]).to(dtype=self.vae_encoder.dtype)
            
            mask_dtype = mask.dtype
            mask = mask.float()
            mask = torch.nn.functional.interpolate(
                mask, size=(dummy_vae_latent.shape[-2], dummy_vae_latent.shape[-1]), mode="nearest"
            )
            mask = mask.to(dtype=mask_dtype)
            mask = mask.repeat(1, dummy_vae_latent.shape[1], 1, 1)

        assert len(mask.shape) == 4 and len(dummy_vae_latent.shape) == 4

        return vae_latent, mask.squeeze(), image_path

    def __getitem__(
        self, idx: int, bucketing_resolution:tuple = None
    ) -> Tuple[Tuple[torch.Tensor, torch.Tensor], torch.Tensor, torch.Tensor]:

        if self.do_cache:
            vae_latent = self.vae_latents[idx].sample() * self.vae_scaling_factor
            return self.captions[idx], vae_latent.squeeze().detach(), self.masks[idx].detach()
        else: # Load from disk:
            vae_latent = torch.load(os.path.join(self.data_dir, f"{idx}_vae_latent.pt"))
            vae_latent = vae_latent.sample() * self.vae_scaling_factor
            mask = torch.load(os.path.join(self.data_dir, f"{idx}_mask.pt"))
            caption = self.captions[idx]
            return caption, vae_latent.squeeze().detach(), mask.detach()


