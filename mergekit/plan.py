# Copyright (C) 2023 Charles O. Goddard
#
# This software is free software: you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This software is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program. If not, see http://www.gnu.org/licenses/.

from typing import List, Optional

from mergekit import merge_methods
from mergekit.architecture import ArchitectureInfo
from mergekit.common import ModelReference
from mergekit.config import (
    ConfigReader,
    InputSliceDefinition,
    MergeConfiguration,
    OutputSliceDefinition,
)
from mergekit.graph import Task
from mergekit.merge import MergeOptions
from mergekit.merge_methods import MergeMethod
from mergekit.merge_methods.tokenizer_permute import TokenizerPermutationMerge
from mergekit.tasks import (
    BuildTokenizer,
    FinalizeModel,
    GatherTensors,
    SaveTensor,
    TensorWriterTask,
)


class MergePlanner:
    config: MergeConfiguration
    arch_info: ArchitectureInfo
    clone_tensors: bool
    _writer_task: TensorWriterTask
    _method: MergeMethod
    _tasks: List[Task] = []
    _current_layers: int = 0
    _tokenizer_task: Optional[BuildTokenizer] = None

    def __init__(
        self,
        config: MergeConfiguration,
        arch_info: ArchitectureInfo,
        out_path: str,
        options: MergeOptions,
    ):
        self.config = config
        self.arch_info = arch_info
        self.clone_tensors = options.clone_tensors
        self._method = merge_methods.get(config.merge_method)
        self._writer_task = TensorWriterTask(
            out_path=out_path, max_shard_size=options.out_shard_size
        )

        if config.merge_method:
            self._tokenizer_task = BuildTokenizer(
                merge_config=config, trust_remote_code=options.trust_remote_code
            )

    def plan_tensor(
        self,
        name: str,
        names_in: List[str],
        models: List[ModelReference],
        cfg_reader: ConfigReader,
    ):
        is_embed = name in self.arch_info.embed_weights()
        tensor_merge_method = self._method
        if self._tokenizer_task and is_embed:
            tensor_merge_method = TokenizerPermutationMerge(
                tokenizer_task=self._tokenizer_task
            )

        cfg_g = cfg_reader.for_in_slices(None).for_tensor(name)
        global_params = {}
        for p in tensor_merge_method.parameters():
            global_params[p.name] = cfg_g.parameter(
                p.name, model=None, required=p.required, default=p.default_value
            )

        tensor_params = {}
        for model, name_in in zip(models, names_in):
            tensor_params[model] = {}
            cfg_m = cfg_reader.for_tensor(name_in)
            for p in tensor_merge_method.tensor_parameters():
                tensor_params[model][p.name] = cfg_m.parameter(
                    p.name, model=model, required=p.required, default=p.default_value
                )

        gather_tensors = GatherTensors(
            tensor_names=dict(zip(models, names_in)), dtype=self.config.dtype
        )
        base_model = (
            ModelReference.parse(self.config.base_model)
            if self.config.base_model
            else None
        )

        tensor_task = tensor_merge_method.make_task(
            output_tensor_name=name,
            tensors=gather_tensors,
            parameters=global_params,
            tensor_parameters=tensor_params,
            base_model=base_model,
        )
        save_task = SaveTensor(
            tensor_name=name,
            tensor_task=tensor_task,
            writer_task=self._writer_task,
            clone=self.clone_tensors,
        )
        self._tasks.append(save_task)

    def plan_layer(
        self,
        sources: List[InputSliceDefinition],
        layer_offset: int,
        t: float,
        cfg_reader: ConfigReader,
    ):
        for name_format in self.arch_info.layer_weight_formats():
            name_out = name_format.format(idx=self._current_layers)
            names_in = [
                name_format.format(idx=s.layer_range[0] + layer_offset) for s in sources
            ]

            self.plan_tensor(
                name=name_out,
                names_in=names_in,
                models=[s.model for s in sources],
                cfg_reader=cfg_reader.with_t(t),
            )

    def plan_slice(self, definition: OutputSliceDefinition):
        slice_lengths = [
            s.layer_range[1] - s.layer_range[0] for s in definition.sources
        ]
        if not all(s == slice_lengths[0] for s in slice_lengths):
            raise RuntimeError(
                "All inputs to a slice must contain the same number of layers"
            )
        num_layers = slice_lengths[0]

        cfg_reader = ConfigReader(config=self.config, slice_out=definition, t=0)
        for idx in range(num_layers):
            # compute t for interpolated gradients
            if num_layers > 1:
                t = idx / (num_layers - 1)
            else:
                t = 1

            self.plan_layer(
                definition.sources,
                layer_offset=idx,
                t=t,
                cfg_reader=cfg_reader.for_in_slices(definition.sources),
            )

    def plan(self):
        self._tasks = []

        for weight_name in self.arch_info.pre_weights():
            self.plan_tensor(
                weight_name,
                [weight_name] * len(self.config.slices[0].sources),
                [s.model for s in self.config.slices[0].sources],
                ConfigReader(config=self.config, t=0, tensor_name=weight_name),
            )

        for out_slice in self.config.slices:
            self.plan_slice(out_slice)

        for weight_name in self.arch_info.post_weights():
            self.plan_tensor(
                weight_name,
                [weight_name] * len(self.config.slices[-1].sources),
                [s.model for s in self.config.slices[-1].sources],
                ConfigReader(config=self.config, t=1, tensor_name=weight_name),
            )

        self._tasks.append(
            FinalizeModel(
                tensor_save_tasks=list(self._tasks), writer_task=self._writer_task
            )
        )
        res = list(self._tasks)
        if self._tokenizer_task:
            res.append(self._tokenizer_task)
        return res
