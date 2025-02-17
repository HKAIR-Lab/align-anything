# Copyright 2024 PKU-Alignment Team. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================


import json

from typing import Any, Callable
from typing_extensions import TypedDict  # Python 3.10+

from tqdm import tqdm

import torch
import transformers
from torch.utils.data import Dataset
from torchvision import transforms
from transformers.tokenization_utils import PaddingStrategy, TruncationStrategy

from align_anything.utils.multi_process import get_current_device
from align_anything.utils.template_registry import get_template_class
from align_anything.utils.tools import right_padding
from datasets import load_dataset
import pandas as pd
from datasets import Dataset
from datasets import DatasetDict
import tempfile


__all__ = [
    'PreferenceDataset',
    'PreferenceCollator',
    'PreferenceSample',
    'PreferenceBatch',
    'PreferenceDataset_ours',
    'PreferenceCollator_ours',
    'PreferenceSample_ours',
    'PreferenceBatch_ours',
]


class PreferenceSample(TypedDict, total=True):
    input_ids: torch.LongTensor  # size = (L,)
    labels: torch.LongTensor  # size = (L,)
    pixel_values: torch.LongTensor | None  # size = (B, C, H, W)


class PreferenceBatch(TypedDict, total=True):
    input_ids: torch.LongTensor  # size = (B, L)
    labels: torch.LongTensor  # size = (B, L)
    attention_mask: torch.BoolTensor  # size = (B, L)
    pixel_values: torch.LongTensor | None  # size = (B, C, H, W)


class PreferenceDataset(Dataset):

    def __init__(
        self,
        path: str,
        template: str,
        tokenizer: transformers.PreTrainedTokenizer,
        processor: transformers.ProcessorMixin | transforms.Compose | None = None,
        name: str | None = None,
        size: int | None = None,
        split: str | None = None,
        subset: str | None = None,
        data_files: str | None = None,
        optional_args: list | str = [],
    ):
        super().__init__()
        assert path, f'You must set the valid datasets path! Here is {path}'
        assert template, f'You must set the valid template path! Here is {template}'
        self.tokenizer = tokenizer
        self.processor = processor
        self.template = get_template_class(template)

        if isinstance(optional_args, str):
            optional_args = [optional_args]
            
        with open(path, 'r') as f:
            self.raw_data = json.load(f)


        # 获取当前临时目录
        current_tmp_dir = tempfile.gettempdir()

        print(f"当前临时目录是: {current_tmp_dir}")
        import os
        os.environ['TMPDIR'] = '/aifs4su/chenxinyu/.tmp'
        
        tempfile.tempdir = '/aifs4su/chenxinyu/.tmp'
        current_tmp_dir = tempfile.gettempdir()
        print(f"当前临时目录是: {current_tmp_dir}")

        self.data = self.pre_tokenize()
        if size:
            size = min(size, len(self.data))
            self.data = self.data[:size]
        

    def pre_tokenize(self) -> list[dict[str, torch.tensor]]:
        data = []
        for item in tqdm(self.raw_data, total=len(self.raw_data), desc="Pre-tokenizing and filltering..."):
            return_dict = {}
            formatted_sample = self.template.format_sample(item)
            raw_better_text = ''
            raw_worse_text = ''
            if isinstance(formatted_sample['better_text'], list):
                raw_better_text = self.tokenizer.eos_token.join(formatted_sample['better_text'])
                raw_worse_text = self.tokenizer.eos_token.join(formatted_sample['worse_text'])
            elif isinstance(formatted_sample['better_text'], str):
                raw_better_text = formatted_sample['prompt'] + formatted_sample['better_text'] + self.tokenizer.eos_token
                raw_worse_text = formatted_sample['prompt'] + formatted_sample['worse_text'] + self.tokenizer.eos_token
                raw_better_response = formatted_sample['better_text'] + self.tokenizer.eos_token
                raw_worse_response = formatted_sample['worse_text'] + self.tokenizer.eos_token
            else:
                raise NotImplementedError
            
            return_dict['better_input_ids'] = self.tokenize(raw_better_text)
            return_dict['worse_input_ids'] = self.tokenize(raw_worse_text)
            return_dict['better_response_lens'] = len(self.tokenize(raw_better_response))
            return_dict['worse_response_lens'] = len(self.tokenize(raw_worse_response))
            raw_image = formatted_sample['image']
            return_dict['pixel_values'] = self.processor.image_processor(
                raw_image, return_tensors='pt'
            )['pixel_values'][0]
            
            if self.tokenize(raw_better_text).size(0) < self.tokenizer.model_max_length - 576 and self.tokenize(raw_worse_text).size(0) < self.tokenizer.model_max_length - 576 and not self.template.check_equal(item):
                data.append(return_dict)

        return data


    def preprocess(self, raw_sample: dict[str, Any]) -> PreferenceSample:
        return raw_sample

    def get_collator(self) -> Callable[[list[dict[str, torch.Tensor]]], dict[str, torch.Tensor]]:
        return PreferenceCollator(self.tokenizer.pad_token_id)

    def tokenize(
        self,
        text: str,
        add_special_tokens: bool = True,
        padding: bool | str | PaddingStrategy = PaddingStrategy.DO_NOT_PAD,
        truncation: bool | str | TruncationStrategy = TruncationStrategy.LONGEST_FIRST,
        max_length: int | None = None,
    ) -> torch.LongTensor:  # size = (L,)
        """Tokenize a text string into a tensor representation."""
        if max_length is None:
            max_length = self.tokenizer.model_max_length

        return self.tokenizer(
            text,
            add_special_tokens=add_special_tokens,
            padding=padding,
            max_length=max_length,
            truncation=truncation,
            return_tensors='pt',
        )['input_ids'][0]

    def __getitem__(self, index: int) -> dict[str, torch.Tensor]:
        """Get a tokenized data sample by index."""
        return self.preprocess(self.data[index])

    def __len__(self) -> int:
        """Get the number of samples in the dataset."""
        return len(self.data)


class PreferenceCollator:

    def __init__(self, pad_token_id: int) -> None:
        """Initialize a collator."""
        self.pad_token_id = pad_token_id

    def __call__(self, samples: list[PreferenceSample]) -> tuple[PreferenceBatch]:
        return_dict = {}
        current_device = get_current_device()

        input_ids = [sample['better_input_ids'] for sample in samples] + [
            sample['worse_input_ids'] for sample in samples
        ]  # size = (2 * B, L)
        return_dict['input_ids'] = right_padding(input_ids, padding_value=self.pad_token_id).to(
            current_device
        )  # size = (2 * B, L)

        attention_mask = [
            input_id.new_ones(input_id.size(), dtype=torch.bool) for input_id in input_ids
        ]  # size = (2 * B, L)
        return_dict['attention_mask'] = right_padding(attention_mask, padding_value=0).to(
            current_device
        )  # size = (2 * B, L)
        return_dict['better_response_lens'] = [sample['better_response_lens'] for sample in samples]
        return_dict['worse_response_lens'] = [sample['worse_response_lens'] for sample in samples]
        return_dict['response_lens'] = return_dict['better_response_lens'] + return_dict['worse_response_lens']
        if 'pixel_values' in samples[0].keys():

            a = return_dict['attention_mask'].shape[0]

            if samples[0]['pixel_values'].dim() == 4:
                # init list for pixel_values
                ori_patches = [
                    sample['pixel_values'].to(current_device).size(0) for sample in samples
                ]
                ori_patches_tensor = torch.tensor(ori_patches)
                double_ori_patches_tensor = torch.cat(
                    [ori_patches_tensor, ori_patches_tensor], dim=0
                )
                return_dict['image_sizes'] = double_ori_patches_tensor.to(current_device)

                _pixel_values_list = []
                for sample in samples:
                    pixel_values = sample['pixel_values']  # size = (P, C, H, W)
                    _pixel_values_list.append(pixel_values)

                pixel_values_tensor = torch.cat(_pixel_values_list, dim=0).to(current_device)
                double_stacked = torch.cat([pixel_values_tensor, pixel_values_tensor], dim=0)
                return_dict['pixel_values'] = double_stacked.to(current_device)

                # size = (P1+P2+...+P_n+P1+P2+...+P_n, C, H, W)

            else:
                # original code for non-patches
                pixel_values_tensor = torch.stack(
                    [sample['pixel_values'] for sample in samples]
                ).to(current_device)
                double_stacked = torch.cat([pixel_values_tensor, pixel_values_tensor], dim=0)
                return_dict['pixel_values'] = double_stacked.to(current_device)

        return return_dict


class PreferenceSample_ours(TypedDict, total=True):
    input_ids: torch.LongTensor  # size = (L,)
    labels: torch.LongTensor  # size = (L,)
    pixel_values: torch.LongTensor | None  # size = (B, C, H, W)
    is_better_safe: str
    is_worse_safe: str


class PreferenceBatch_ours(TypedDict, total=True):
    input_ids: torch.LongTensor  # size = (B, L)
    labels: torch.LongTensor  # size = (B, L)
    attention_mask: torch.BoolTensor  # size = (B, L)
    pixel_values: torch.LongTensor | None  # size = (B, C, H, W)
    is_better_safe: list # size = (B)
    is_worse_safe: list  # size = (B)


class PreferenceDataset_ours(Dataset):

    def __init__(
        self,
        path: str,
        template: str,
        tokenizer: transformers.PreTrainedTokenizer,
        processor: transformers.ProcessorMixin | transforms.Compose | None = None,
        name: str | None = None,
        size: int | None = None,
        split: str | None = None,
        subset: str | None = None,
        data_files: str | None = None,
        optional_args: list | str = [],
    ):
        super().__init__()
        assert path, f'You must set the valid datasets path! Here is {path}'
        assert template, f'You must set the valid template path! Here is {template}'
        self.tokenizer = tokenizer
        self.processor = processor
        self.template = get_template_class(template)

        if isinstance(optional_args, str):
            optional_args = [optional_args]
        with open(path, 'r') as f:
            self.raw_data = json.load(f)
        #self.raw_data = self.raw_data[:400]
        self.data = self.pre_tokenize()
        if size:
            size = min(size, len(self.data))
            self.data = self.data.select(range(int(size)))
        

    def pre_tokenize(self) -> list[dict[str, torch.tensor]]:
        data = []
        for item in tqdm(self.raw_data, total=len(self.raw_data), desc="Pre-tokenizing and filltering..."):
            return_dict = {}
            formatted_sample = self.template.format_sample(item)
            raw_better_text = ''
            raw_worse_text = ''
            if isinstance(formatted_sample['better_text'], list):
                raw_better_text = self.tokenizer.eos_token.join(formatted_sample['better_text'])
                raw_worse_text = self.tokenizer.eos_token.join(formatted_sample['worse_text'])
            elif isinstance(formatted_sample['better_text'], str):
                raw_better_text = formatted_sample['prompt'] + formatted_sample['better_text'] + self.tokenizer.eos_token
                raw_worse_text = formatted_sample['prompt'] + formatted_sample['worse_text'] + self.tokenizer.eos_token
                raw_better_response = formatted_sample['better_text'] + self.tokenizer.eos_token
                raw_worse_response = formatted_sample['worse_text'] + self.tokenizer.eos_token
            else:
                raise NotImplementedError
            return_dict['is_better_safe'] = formatted_sample['is_better_safe']
            return_dict['is_worse_safe'] = formatted_sample['is_worse_safe']
            return_dict['better_input_ids'] = self.tokenize(raw_better_text)
            return_dict['worse_input_ids'] = self.tokenize(raw_worse_text)
            return_dict['better_response_lens'] = len(self.tokenize(raw_better_response))
            return_dict['worse_response_lens'] = len(self.tokenize(raw_worse_response))
            raw_image = formatted_sample['image']
            return_dict['pixel_values'] = self.processor.image_processor(
                raw_image, return_tensors='pt'
            )['pixel_values'][0]
            
            if self.tokenize(raw_better_text).size(0) < self.tokenizer.model_max_length - 576 and self.tokenize(raw_worse_text).size(0) < self.tokenizer.model_max_length - 576 and not self.template.check_equal(item):
                data.append(return_dict)

        return data


    def preprocess(self, raw_sample: dict[str, Any]) -> PreferenceSample:
        return raw_sample

    def get_collator(self) -> Callable[[list[dict[str, torch.Tensor]]], dict[str, torch.Tensor]]:
        return PreferenceCollator_ours(self.tokenizer.pad_token_id)

    def tokenize(
        self,
        text: str,
        add_special_tokens: bool = True,
        padding: bool | str | PaddingStrategy = PaddingStrategy.DO_NOT_PAD,
        truncation: bool | str | TruncationStrategy = TruncationStrategy.LONGEST_FIRST,
        max_length: int | None = None,
    ) -> torch.LongTensor:  # size = (L,)
        """Tokenize a text string into a tensor representation."""
        if max_length is None:
            max_length = self.tokenizer.model_max_length

        return self.tokenizer(
            text,
            add_special_tokens=add_special_tokens,
            padding=padding,
            max_length=max_length,
            truncation=truncation,
            return_tensors='pt',
        )['input_ids'][0]

    def __getitem__(self, index: int) -> dict[str, torch.Tensor]:
        """Get a tokenized data sample by index."""
        raw_sample = self.raw_data[index]
        # print("###############")
        # print("now index is: ", index)
        # print(raw_sample)
        # print("###############")
        return self.preprocess(self.data[index])

    def __len__(self) -> int:
        """Get the number of samples in the dataset."""
        return len(self.data)


class PreferenceCollator_ours:

    def __init__(self, pad_token_id: int) -> None:
        """Initialize a collator."""
        self.pad_token_id = pad_token_id

    def __call__(self, samples: list[PreferenceSample]) -> tuple[PreferenceBatch]:
        return_dict = {}
        current_device = get_current_device()

        input_ids = [sample['better_input_ids'] for sample in samples] + [
            sample['worse_input_ids'] for sample in samples
        ]  # size = (2 * B, L)
        return_dict['input_ids'] = right_padding(input_ids, padding_value=self.pad_token_id).to(
            current_device
        )  # size = (2 * B, L)

        attention_mask = [
            input_id.new_ones(input_id.size(), dtype=torch.bool) for input_id in input_ids
        ]  # size = (2 * B, L)
        return_dict['attention_mask'] = right_padding(attention_mask, padding_value=0).to(
            current_device
        )  # size = (2 * B, L)
        return_dict['is_better_safe'] = [sample['is_better_safe'] for sample in samples]
        return_dict['is_worse_safe'] = [sample['is_worse_safe'] for sample in samples]
        return_dict['better_response_lens'] = [sample['better_response_lens'] for sample in samples]
        return_dict['worse_response_lens'] = [sample['worse_response_lens'] for sample in samples]
        return_dict['response_lens'] = return_dict['better_response_lens'] + return_dict['worse_response_lens']
        if 'pixel_values' in samples[0].keys():

            a = return_dict['attention_mask'].shape[0]

            if samples[0]['pixel_values'].dim() == 4:
                # init list for pixel_values
                ori_patches = [
                    sample['pixel_values'].to(current_device).size(0) for sample in samples
                ]
                ori_patches_tensor = torch.tensor(ori_patches)
                double_ori_patches_tensor = torch.cat(
                    [ori_patches_tensor, ori_patches_tensor], dim=0
                )
                return_dict['image_sizes'] = double_ori_patches_tensor.to(current_device)

                _pixel_values_list = []
                for sample in samples:
                    pixel_values = sample['pixel_values']  # size = (P, C, H, W)
                    _pixel_values_list.append(pixel_values)

                pixel_values_tensor = torch.cat(_pixel_values_list, dim=0).to(current_device)
                double_stacked = torch.cat([pixel_values_tensor, pixel_values_tensor], dim=0)
                return_dict['pixel_values'] = double_stacked.to(current_device)

                # size = (P1+P2+...+P_n+P1+P2+...+P_n, C, H, W)

            else:
                # original code for non-patches
                pixel_values_tensor = torch.stack(
                    [sample['pixel_values'] for sample in samples]
                ).to(current_device)
                double_stacked = torch.cat([pixel_values_tensor, pixel_values_tensor], dim=0)
                return_dict['pixel_values'] = double_stacked.to(current_device)

        return return_dict
