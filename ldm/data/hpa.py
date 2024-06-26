import webdataset as wds
from torch.utils.data import Dataset
import cv2
import albumentations
import PIL
from functools import partial
import numpy as np
import torchvision.transforms.functional as TF
from PIL import Image
from tqdm import tqdm
from ldm.modules.image_degradation import degradation_fn_bsr, degradation_fn_bsr_light
import os
import random
import torch
import torch.nn as nn
from functools import partial
from einops import rearrange
from ldm.util import instantiate_from_config
from torchvision.utils import make_grid
import json
import h5py

try:
   import cPickle as pickle
except:
   import pickle

HPA_DATA_ROOT = os.environ.get("HPA_DATA_ROOT", "/data/wei/hpa-webdataset-all-composite")

class HPACombineDataset(Dataset):
    def __init__(self, filename, include_metadata=False, length=80000, protein_embedding="bert"):
        super().__init__()
        self.include_metadata = include_metadata
        assert not filename.endswith(".tar")
        if not include_metadata:
            url = f"{HPA_DATA_ROOT}/{filename}_img.tar"
            dataset = wds.WebDataset(url, nodesplitter=wds.split_by_node).decode().to_tuple("__key__", "img.pyd")
            self.dataset_iter = iter(zip(dataset))
        else:
            assert filename == "webdataset"
            url = f"{HPA_DATA_ROOT}/{filename}_img.tar"
            dataset1 = wds.WebDataset(url, nodesplitter=wds.split_by_node).decode().to_tuple("__key__", "img.pyd")

            url = f"{HPA_DATA_ROOT}/{filename}_info.tar"
            dataset2 = wds.WebDataset(url, nodesplitter=wds.split_by_node).decode().to_tuple("__key__", "info.json")
            
            if protein_embedding == "bert":
                url = f"{HPA_DATA_ROOT}/{filename}_bert.tar"
                dataset3 = wds.WebDataset(url, nodesplitter=wds.split_by_node).decode().to_tuple("__key__", "bert.pyd")
            elif protein_embedding == "t5":
                url = f"{HPA_DATA_ROOT}/{filename}_t5.tar"
                dataset3 = wds.WebDataset(url, nodesplitter=wds.split_by_node).decode().to_tuple("__key__", "t5.pyd")
            else:
                raise NotImplementedError()
            self.dataset_iter = iter(zip(dataset1, dataset2, dataset3))
        
        self._generator = self.sample_generator()
            
        self.length = length

    def __len__(self):
        return self.length

    def sample_generator(self):
        for ret in self.dataset_iter:
            if not self.include_metadata:
                imgd = ret[0]
                yield {"file_path_": imgd[0], "image": imgd[1]}
            else:
                imgd, infod, embedd = ret
                assert imgd[0] == infod[0] and imgd[0] == embedd[0]
                yield {"file_path_": imgd[0], "image": imgd[1], "info": infod[1], "embed": embedd[1]}


    def __getitem__(self, i):
        return next(self._generator)


location_mapping = {"Actin filaments": 0, "Aggresome": 1, "Cell Junctions": 2, "Centriolar satellite": 3, "Centrosome": 4, "Cytokinetic bridge": 5, "Cytoplasmic bodies": 6, "Cytosol": 7, "Endoplasmic reticulum": 8, "Endosomes": 9, "Focal adhesion sites": 10, "Golgi apparatus": 11, "Intermediate filaments": 12, "Lipid droplets": 13, "Lysosomes": 14, "Microtubule ends": 15, "Microtubules": 16, "Midbody": 17, "Midbody ring": 18, "Mitochondria": 19, "Mitotic chromosome": 20, "Mitotic spindle": 21, "Nuclear bodies": 22, "Nuclear membrane": 23, "Nuclear speckles": 24, "Nucleoli": 25, "Nucleoli fibrillar center": 26, "Nucleoplasm": 27, "Peroxisomes": 28, "Plasma membrane": 29, "Rods & Rings": 30, "Vesicles": 31, "nan": 32}
cellline_mapping = {"A-431": 0, "A549": 1, "AF22": 2, "ASC TERT1": 3, "BJ": 4, "CACO-2": 5, "EFO-21": 6, "HAP1": 7, "HDLM-2": 8, "HEK 293": 9, "HEL": 10, "HTC": 11, "HUVEC TERT2": 12, "HaCaT": 13, "HeLa": 14, "Hep G2": 15, "JURKAT": 16, "K-562": 17, "LHCN-M2": 18, "MCF7": 19, "NB-4": 20, "NIH 3T3": 21, "OE19": 22, "PC-3": 23, "REH": 24, "RH-30": 25, "RPTEC TERT1": 26, "RT4": 27, "SH-SY5Y": 28, "SK-MEL-30": 29, "SiHa": 30, "SuSa": 31, "THP-1": 32, "U-2 OS": 33, "U-251 MG": 34, "Vero": 35, "hTCEpi": 36}

class HPACombineDatasetMetadata():
    def __init__(self, filename="webdataset", channels=None, include_metadata=True, return_info=False, size=None, length=80000, random_crop=False, protein_embedding="bert"):
        self.size = size
        self.random_crop = random_crop
        self.base = HPACombineDataset(filename, include_metadata=include_metadata, length=length, protein_embedding=protein_embedding)
        if self.size is not None and self.size > 0:
            self.rescaler = albumentations.SmallestMaxSize(max_size = self.size)
            if not self.random_crop:
                self.cropper = albumentations.CenterCrop(height=self.size,width=self.size)
            else:
                self.cropper = albumentations.RandomCrop(height=self.size,width=self.size)
            self.preprocessor = albumentations.Compose([self.rescaler, self.cropper])
        else:
            self.preprocessor = lambda **kwargs: kwargs
        if channels is None:
            self.channels = [0, 1, 2]
        else:
            self.channels = channels
        self.return_info = return_info

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

    def prepare_sample(self, example):
        all_channels = example["image"]
        image = all_channels[:, :, self.channels]
        info = example["info"]
        embed = example["embed"]
        ref = all_channels[:, :, [0, 3, 2]] # reference channels: MT, ER, DAPI

        # loc_labels = list(map(lambda n: location_mapping[n] if n in location_mapping else -1, str(info["locations"]).split(',')))
        # create one-hot encoding for the labels
        # locations_encoding = np.zeros((len(location_mapping), ), dtype=np.float32)
        # locations_encoding[loc_labels] = 1
        
        # create one-hot encoding for the cell line
        cellline_encoding = np.zeros((len(cellline_mapping) + 1, ), dtype=np.float32)
        cellline_encoding[cellline_mapping[info['atlas_name']]] = 1

        ret = {
            "image": self.preprocess_image(image),
            # "class_label": locations_encoding,
            "cell-line": cellline_encoding,
            "ref-image": self.preprocess_image(ref),
            "embed": embed,
            "condition_caption": f"{info['gene_names']}/{info['atlas_name']}",
            "location_caption": f"{info['locations']}",
        }
        
        if self.return_info:
            ret['info'] = info

        return ret

    def __getitem__(self, i):
        example = self.base[i]
        return self.prepare_sample(example)


TOTAL_LENGTH = 247678

def dump_info(info_pickle_path):
        # dump info
    url = f"{HPA_DATA_ROOT}/webdataset_info.tar"
    dataset_info = wds.WebDataset(url, nodesplitter=wds.split_by_node).decode().to_tuple("__key__", "info.json")
    info_list = []
    for _, info in tqdm(dataset_info, total=TOTAL_LENGTH):
        info_list.append(info)
    assert len(info_list) == TOTAL_LENGTH
    with open(info_pickle_path, 'wb') as fp:
        pickle.dump(info_list, fp)

class HPACombineDatasetMetadataInMemory():

    samples_dict = {}
    
    @staticmethod
    def generate_cache(cache_file, *args, total_length=None, **kwargs):
        print("Reading data into memory, this may take a while...")
        dataset = HPACombineDatasetMetadata(*args, return_info=True, **kwargs)
        gen = dataset.base.sample_generator()
        samples = []
        total_length = total_length or TOTAL_LENGTH
        for idx in tqdm(range(total_length), total=total_length):
            sample = next(gen)
            samples.append(dataset.prepare_sample(sample))
        with open(cache_file, 'wb') as fp:
            pickle.dump(samples, fp)

    def __init__(self, cache_file, seed=123, train_split_ratio=0.95, group='train', channels=None, include_location=False, include_densenet_embedding=False, return_info=False, filter_func=None, dump_to_file=None, rotate_and_flip=False, split_by_indexes=None, use_uniprot_embedding=False):
        if cache_file in HPACombineDatasetMetadataInMemory.samples_dict:
            self.samples = HPACombineDatasetMetadataInMemory.samples_dict[cache_file]
        else:
            if os.path.exists(cache_file):
                print(f"Loading data from cache file {cache_file}, this may take a while...")
                with open(cache_file, 'rb') as fp:
                    self.samples = pickle.load(fp)
                
                if include_densenet_embedding:
                    assert split_by_indexes is None
                    with open("/data/wei/hpa-webdataset-all-composite/HPACombineDatasetInfo-indexes-densenet-features-avg.json", "r") as f:
                        multi_avg_embeddings = json.load(f)
                    with open("/data/wei/hpa-webdataset-all-composite/HPACombineDatasetInfo-densenet-features-avg.pickle", "rb") as f:
                        densent_features_avg = pickle.load(f)

                    sample_count = len(self.samples)
                    # debug cache file example: HPACombineDatasetMetadataInMemory-256-1000-t5.pickle
                    if "-1000" not in cache_file:
                        assert sample_count == len(densent_features_avg)
                    # for debug, we have less samples in the samples
                    if len(densent_features_avg)> sample_count:
                        multi_avg_embeddings = [idx for idx in multi_avg_embeddings if idx < sample_count]
                    
                    for idx in multi_avg_embeddings:
                        self.samples[idx]['densent_avg'] = densent_features_avg[idx]
                    
                    self.samples = [self.samples[idx] for idx in multi_avg_embeddings]
                    if 'embed' in self.samples[0]:
                        for sample in self.samples:
                            del sample['embed']

                # Patch the generated pickle file to include info
                # with open(f"{HPA_DATA_ROOT}/HPACombineDatasetInfo.pickle", 'rb') as fp:
                #     info_list = pickle.load(fp)
                # assert len(info_list) == len(self.samples)
                # for i in tqdm(range(len(self.samples)), total=len(self.samples)):
                #     self.samples[i]["info"] = info_list[i]
                # with open(cache_file, 'wb') as fp:
                #     pickle.dump(self.samples, fp)
                # print("Data loaded")
            else:
                raise Exception(f"Cache file not found {cache_file}")
            HPACombineDatasetMetadataInMemory.samples_dict[cache_file] = self.samples
        
        if filter_func and split_by_indexes is None:
            if filter_func == 'has_location':
                filter_func = lambda x: int( x['info']['status']) == 35 and x['info']['Ab state'] == 'IF_FINISHED' and str(x['info']['locations']) != "nan"

            self.samples = list(filter(filter_func, self.samples))
        
        if use_uniprot_embedding:
            uniprot_indexes = []
            with h5py.File(use_uniprot_embedding, "r") as file:
                for idx, sample in enumerate(self.samples):
                    if len(sample['info']['sequences']) > 0:
                        for seq in sample['info']['sequences']:
                            prot_id = seq.split('|')[1]
                            if prot_id in file:
                                sample['prot_id'] = prot_id
                                sample['embed'] = np.array(file[prot_id])
                                uniprot_indexes.append(idx)
        
        if dump_to_file:
            assert dump_to_file != cache_file, "please do not overwrite the cache file"
            with open(dump_to_file, 'wb') as fp:
                pickle.dump(self.samples, fp)

        self.include_densenet_embedding = include_densenet_embedding
        self.channels = channels
        assert "info" in self.samples[0]

        self.include_location = include_location
        self.return_info = return_info
        self.rotate_and_flip = rotate_and_flip
        if rotate_and_flip:
            self.preprocessor = albumentations.Compose(
                [albumentations.Rotate(limit=180, border_mode=cv2.BORDER_REFLECT_101, p=1.0, interpolation=cv2.INTER_NEAREST),
                albumentations.HorizontalFlip(p=0.5)])
        self.length = len(self.samples)
        assert group in ['train', 'validation']
        if split_by_indexes is None:
            assert train_split_ratio < 1 and train_split_ratio > 0
            random.seed(seed)
            indexes = list(range(self.length))
            random.shuffle(indexes)
            size = int(train_split_ratio * self.length)
            if group == 'train':
                self.indexes = indexes[:size]
            else:
                self.indexes = indexes[size:]
            if use_uniprot_embedding:
                self.indexes = list(filter(lambda i: i in uniprot_indexes, self.indexes))
        else:
            assert train_split_ratio is None, "train_split_ratio should not be None when split_by_indexes is used"
            with open(split_by_indexes, "r") as in_file:
                idcs = json.load(in_file)
            # assert len(self.samples) == len(idcs['train']) + len(idcs['validation'])
            self.indexes = list(filter(lambda i: i < self.length, idcs[group]))
            if use_uniprot_embedding:
                self.indexes = list(filter(lambda i: i in uniprot_indexes, self.indexes))
        print(f"Dataset group: {group}, length: {len(self.indexes)}, image channels: {self.channels or [0, 1, 2]}")

    def __len__(self):
        return len(self.indexes)

    def __getitem__(self, i):
        sample = self.samples[self.indexes[i]].copy()
        if self.channels:
            sample['image'] = sample['image'][:, :, self.channels]
        info = sample["info"]
        sample["condition_caption"] = f"{info['gene_names']}/{info['atlas_name']}"
        sample["location_caption"] = f"{info['locations']}"
        if self.include_location:
            loc_labels = list(map(lambda n: location_mapping[n] if n in location_mapping else -1, str(info["locations"]).split(',')))
            # create one-hot encoding for the labels
            locations_encoding = np.zeros((len(location_mapping), ), dtype=np.float32)
            locations_encoding[loc_labels] = 1
            sample["location_classes"] = locations_encoding
        if self.rotate_and_flip:
            # make sure the pixel values should be [0, 1], but the sample image is ranging from -1 to 1
            transformed = self.preprocessor(image=(sample["image"]+1)/2, mask=(sample["ref-image"]+1)/2)
            # restore the range from [0, 1] to [-1, 1]
            sample["image"] = transformed["image"]*2 -1
            sample["ref-image"] = transformed["mask"]*2 -1
        if not self.return_info:
            del sample["info"] # Remove info to avoid issue in the dataloader
        return sample


class HPACombineDatasetSR(Dataset):
    def __init__(self, filename, size=None, length=80000, channels=None,
                 degradation=None, downscale_f=4, min_crop_f=0.5, max_crop_f=1.,
                 random_crop=True, protein_embedding="bert"):
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
        if channels is None:
            self.channels = [0, 1, 2]
        else:
            self.channels = channels
        self.base = HPACombineDataset(filename, include_metadata=False, length=length, protein_embedding=protein_embedding)
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
        image = image[:, :, self.channels]

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
    

class ClassEmbedder(nn.Module):
    def __init__(self, embed_dim, n_classes=1000, key='class'):
        super().__init__()
        self.key = key
        self.embedding = nn.Embedding(n_classes, embed_dim)

    def forward(self, batch, key=None):
        if key is None:
            key = self.key
        # this is for use in crossattn
        c = batch[key][:, None]
        c = self.embedding(c)
        return c

class HPAClassEmbedder(nn.Module):
    def __init__(self, include_location=False, include_ref_image=False, include_cellline=True, include_embed=False, use_loc_embedding=True, image_embedding_model=None, include_densenet_embedding=False):
        super().__init__()
        self.include_location = include_location
        self.include_ref_image = include_ref_image
        self.include_embed = include_embed
        self.include_cellline = include_cellline
        self.use_loc_embedding = use_loc_embedding
        self.include_densenet_embedding = include_densenet_embedding
        if self.use_loc_embedding:
            self.loc_embedding = nn.Sequential(
                nn.Linear(len(location_mapping.keys()), 128),
                nn.ReLU(),
                nn.Linear(128, 128),
            )
        
        if image_embedding_model:
            assert not isinstance(image_embedding_model, dict)
            self.image_embedding_model = instantiate_from_config(image_embedding_model)

    def forward(self, batch, key=None):
        embed = []
        if self.include_cellline:
            embed.append(batch["cell-line"])
        if self.include_embed:
            embed.append(batch["embed"])
        if self.include_location:
            if self.use_loc_embedding:
                embeder = self.loc_embedding.to(batch["location_classes"].device)
                embed.append(embeder(batch["location_classes"]))
            else:
                embed.append(batch["location_classes"])
        if self.include_densenet_embedding:
            embed.append(batch["densent_avg"])

        if self.include_ref_image:
            image = batch["ref-image"]
            assert image.shape[3] == 3
            image = rearrange(image, 'b h w c -> b c h w').contiguous()
            with torch.no_grad():
                img_embed = self.image_embedding_model.encode(image)
            if torch.any(torch.isnan(img_embed)):
                raise Exception("NAN values encountered in the image embedding")
            return {"c_concat": [img_embed], "c_crossattn": embed}
        return {"c_crossattn": embed}

    def decode(self, c):
        condition_row = c['c_concat']
        assert len(condition_row) == 1
        with torch.no_grad():
            condition_rec_row = [self.image_embedding_model.decode(cond) for cond in condition_row]
        n_imgs_per_row = len(condition_rec_row)
        condition_rec_row = torch.stack(condition_rec_row)  # n_log_step, n_row, C, H, W
        condition_grid = rearrange(condition_rec_row, 'n b c h w -> b n c h w')
        condition_grid = rearrange(condition_grid, 'b n c h w -> (b n) c h w')
        condition_grid = make_grid(condition_grid, nrow=n_imgs_per_row)
        return condition_grid


class HPAHybridEmbedder(nn.Module):
    def __init__(self, image_embedding_model, include_location=False):
        super().__init__()
        assert not isinstance(image_embedding_model, dict)
        self.image_embedding_model = instantiate_from_config(image_embedding_model)
        self.include_location = include_location

    def forward(self, batch, key=None):
        image = batch["ref-image"]
        assert image.shape[3] == 3
        image = rearrange(image, 'b h w c -> b c h w').contiguous()
        with torch.no_grad():
            img_embed = self.image_embedding_model.encode(image)
        if torch.any(torch.isnan(img_embed)):
            raise Exception("NAN values encountered in the image embedding")
        # embed = batch["embed"]
        cellline = batch["cell-line"]
        # cross_embed = [embed, celline]
        cross_embed = [cellline]
        if self.include_location:
            cross_embed.append(batch["location_classes"])
        return {"c_concat": [img_embed], "c_crossattn": cross_embed}
    
    def decode(self, c):
        condition_row = c['c_concat']
        assert len(condition_row) == 1
        with torch.no_grad():
            condition_rec_row = [self.image_embedding_model.decode(cond) for cond in condition_row]
        n_imgs_per_row = len(condition_rec_row)
        condition_rec_row = torch.stack(condition_rec_row)  # n_log_step, n_row, C, H, W
        condition_grid = rearrange(condition_rec_row, 'n b c h w -> b n c h w')
        condition_grid = rearrange(condition_grid, 'b n c h w -> (b n) c h w')
        condition_grid = make_grid(condition_grid, nrow=n_imgs_per_row)
        return condition_grid


if __name__ == "__main__":
    # HPACombineDatasetMetadataInMemory(seed=123, train_split_ratio=0.95, group='train', cache_file=f"{HPA_DATA_ROOT}/HPACombineDatasetMetadataInMemory-256.pickle", channels= [1, 1, 1],
    #     filter_func="has_location", dump_to_file=f"{HPA_DATA_ROOT}/HPACombineDatasetMetadataInMemory-256-has-location.pickle")
    # HPACombineDatasetMetadataInMemory.generate_cache(f"{HPA_DATA_ROOT}/HPACombineDatasetMetadataInMemory-256-1000.pickle", size=256, total_length=1000)
    # HPACombineDatasetMetadataInMemory.generate_cache(f"{HPA_DATA_ROOT}/HPACombineDatasetMetadataInMemory-256-1000-t5.pickle", size=256, total_length=1000, protein_embedding="t5")
    HPACombineDatasetMetadataInMemory.generate_cache(f"{HPA_DATA_ROOT}/HPACombineDatasetMetadataInMemory-256-t5.pickle", size=256, protein_embedding="t5")
    # HPACombineDatasetMetadataInMemory.generate_cache(f"{HPA_DATA_ROOT}/HPACombineDatasetMetadataInMemory-256-t5.pickle", size=256, protein_embedding="t5")
    # dump_info(f"{HPA_DATA_ROOT}/HPACombineDatasetInfo.pickle")