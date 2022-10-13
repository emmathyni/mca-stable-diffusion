import webdataset as wds
from torch.utils.data import Dataset
import cv2
import albumentations
import PIL
from functools import partial
import numpy as np
import torchvision.transforms.functional as TF
from PIL import Image
from ldm.modules.image_degradation import degradation_fn_bsr, degradation_fn_bsr_light

class HPACombineDataset(Dataset):
    def __init__(self, filename, include_metadata=False, length=80000):
        super().__init__()
        self.include_metadata = include_metadata
        assert not filename.endswith(".tar")
        if not include_metadata:
            url = f"/data/wei/hpa-webdataset-all-composite/{filename}_img.tar"
            dataset = wds.WebDataset(url, nodesplitter=wds.split_by_node).decode().to_tuple("__key__", "img.pyd")
            self.dataset_iter = iter(zip(dataset))
        else:
            assert filename == "webdataset"
            url = f"/data/wei/hpa-webdataset-all-composite/{filename}_img.tar"
            dataset1 = wds.WebDataset(url).decode().to_tuple("__key__", "img.pyd")

            url = f"/data/wei/hpa-webdataset-all-composite/{filename}_info.tar"
            dataset2 = wds.WebDataset(url).decode().to_tuple("__key__", "info.json")

            url = f"/data/wei/hpa-webdataset-all-composite/{filename}_embed.tar"
            dataset3 = wds.WebDataset(url).decode().to_tuple("__key__", "deepgoplus.pyd", "desenet.pyd")
            self.dataset_iter = iter(zip(dataset1, dataset2, dataset3))
            
            
        self.length = length

    def __len__(self):
        return self.length

    def __getitem__(self, i):
        ret = next(self.dataset_iter)
        if not self.include_metadata:
            imgd = ret[0]
            return {"file_path_": imgd[0], "image": imgd[1]}
        else:
            imgd, infod, embedd = ret
            assert imgd[0] == infod[0] and imgd[0] == embedd[0]
            return {"file_path_": imgd[0], "image": imgd[1], "info": infod[1], "seq_embed": embedd[1], "loc_embed": embedd[2]}

location_mapping = {"Actin filaments": 0, "Aggresome": 1, "Cell Junctions": 2, "Centriolar satellite": 3, "Centrosome": 4, "Cytokinetic bridge": 5, "Cytoplasmic bodies": 6, "Cytosol": 7, "Endoplasmic reticulum": 8, "Endosomes": 9, "Focal adhesion sites": 10, "Golgi apparatus": 11, "Intermediate filaments": 12, "Lipid droplets": 13, "Lysosomes": 14, "Microtubule ends": 15, "Microtubules": 16, "Midbody": 17, "Midbody ring": 18, "Mitochondria": 19, "Mitotic chromosome": 20, "Mitotic spindle": 21, "Nuclear bodies": 22, "Nuclear membrane": 23, "Nuclear speckles": 24, "Nucleoli": 25, "Nucleoli fibrillar center": 26, "Nucleoplasm": 27, "Peroxisomes": 28, "Plasma membrane": 29, "Rods & Rings": 30, "Vesicles": 31, "nan": 32}

class HPACombineDatasetMetadata():
    def __init__(self, filename="webdataset", include_metadata=True, size=None, length=80000, random_crop=False):
        self.size = size
        self.random_crop = random_crop
        self.base = HPACombineDataset(filename, include_metadata=include_metadata, length=length)
        if self.size is not None and self.size > 0:
            self.rescaler = albumentations.SmallestMaxSize(max_size = self.size)
            if not self.random_crop:
                self.cropper = albumentations.CenterCrop(height=self.size,width=self.size)
            else:
                self.cropper = albumentations.RandomCrop(height=self.size,width=self.size)
            self.preprocessor = albumentations.Compose([self.rescaler, self.cropper])
        else:
            self.preprocessor = lambda **kwargs: kwargs

    def preprocess_image(self, image):
        # image = np.load(image_path).squeeze(0)  # 3 x 1024 x 1024
        # image = np.transpose(image, (1,2,0))
        assert image.shape[2] in [3, 4]
        image = Image.fromarray(image, mode="RGB")
        image = np.array(image).astype(np.uint8)
        image = self.preprocessor(image=image)["image"]
        image = (image/127.5 - 1.0).astype(np.float32)
        return image

    def __len__(self):
        return len(self.base)

    def __getitem__(self, i):
        example = self.base[i]
        image = example["image"]
        image = image[:, :, :3]
        info = example["info"]

        loc_labels = list(map(lambda n: location_mapping[n] if n in location_mapping else -1, str(info["locations"]).split(',')))
        # create one-hot encoding for the labels
        locations_encoding = np.zeros((len(location_mapping) + 1, ), dtype=np.float32)
        locations_encoding[loc_labels] = 1

        return {
            "image": self.preprocess_image(image),
            "class_label": locations_encoding,
            "info": info,
        }

class HPACombineDatasetSR(Dataset):
    def __init__(self, filename, size=None, length=80000,
                 degradation=None, downscale_f=4, min_crop_f=0.5, max_crop_f=1.,
                 random_crop=True):
        """
        Imagenet Superresolution Dataloader
        Performs following ops in order:
        1.  crops a crop of size s from image either as random or center crop
        2.  resizes crop to size with cv2.area_interpolation
        3.  degrades resized crop with degradation_fn

        :param size: resizing to size after cropping
        :param degradation: degradation_fn, e.g. cv_bicubic or bsrgan_light
        :param downscale_f: Low Resolution Downsample factor
        :param min_crop_f: determines crop size s,
          where s = c * min_img_side_len with c sampled from interval (min_crop_f, max_crop_f)
        :param max_crop_f: ""
        :param data_root:
        :param random_crop:
        """
        self.base = HPACombineDataset(filename, include_metadata=False, length=length)
        assert size
        assert (size / downscale_f).is_integer()
        self.size = size
        self.LR_size = int(size / downscale_f)
        self.min_crop_f = min_crop_f
        self.max_crop_f = max_crop_f
        assert(max_crop_f <= 1.)
        self.center_crop = not random_crop

        self.image_rescaler = albumentations.SmallestMaxSize(max_size=size, interpolation=cv2.INTER_AREA)

        self.pil_interpolation = False # gets reset later if incase interp_op is from pillow

        if degradation == "bsrgan":
            self.degradation_process = partial(degradation_fn_bsr, sf=downscale_f)

        elif degradation == "bsrgan_light":
            self.degradation_process = partial(degradation_fn_bsr_light, sf=downscale_f)

        else:
            interpolation_fn = {
            "cv_nearest": cv2.INTER_NEAREST,
            "cv_bilinear": cv2.INTER_LINEAR,
            "cv_bicubic": cv2.INTER_CUBIC,
            "cv_area": cv2.INTER_AREA,
            "cv_lanczos": cv2.INTER_LANCZOS4,
            "pil_nearest": PIL.Image.NEAREST,
            "pil_bilinear": PIL.Image.BILINEAR,
            "pil_bicubic": PIL.Image.BICUBIC,
            "pil_box": PIL.Image.BOX,
            "pil_hamming": PIL.Image.HAMMING,
            "pil_lanczos": PIL.Image.LANCZOS,
            }[degradation]
            

            self.pil_interpolation = degradation.startswith("pil_")

            if self.pil_interpolation:
                self.degradation_process = partial(TF.resize, size=self.LR_size, interpolation=TF.InterpolationMode.NEAREST)

            else:
                self.degradation_process = albumentations.SmallestMaxSize(max_size=self.LR_size,
                                                                          interpolation=interpolation_fn)

    def __len__(self):
        return len(self.base)

    def __getitem__(self, i):
        example = self.base[i]
        image = example["image"]
        image = image[:, :, :3]

        min_side_len = min(image.shape[:2])
        crop_side_len = min_side_len * np.random.uniform(self.min_crop_f, self.max_crop_f, size=None)
        crop_side_len = int(crop_side_len)

        if self.center_crop:
            self.cropper = albumentations.CenterCrop(height=crop_side_len, width=crop_side_len)

        else:
            self.cropper = albumentations.RandomCrop(height=crop_side_len, width=crop_side_len)

        image = self.cropper(image=image)["image"]
        image = self.image_rescaler(image=image)["image"]

        if self.pil_interpolation:
            image_pil = PIL.Image.fromarray(image)
            LR_image = self.degradation_process(image_pil)
            LR_image = np.array(LR_image).astype(np.uint8)

        else:
            LR_image = self.degradation_process(image=image)["image"]

        example["image"] = (image/127.5 - 1.0).astype(np.float32)
        example["LR_image"] = (LR_image/127.5 - 1.0).astype(np.float32)

        return example