# Copyright 2024 Ant Group Co., Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import inspect
import json
from dataclasses import dataclass
from typing import Any, Callable

from secretflow.device.driver import PYU, SPU, DeviceObject, PYUObject, SPUObject, wait
from secretflow.spec.extend.data_pb2 import DeviceObjectCollection
from secretflow.spec.v1.data_pb2 import DistData, SystemInfo
from secretflow.utils import secure_pickle as pickle

from ..storage import Storage
from .base import IDumper


@dataclass
class Version:
    major: int
    minor: int


class Model(IDumper):
    def __init__(
        self,
        name: str,
        type: str,
        version: Version = None,
        objs: list[DeviceObject] = None,
        public_info: Any = None,
        metadata: dict | None = None,
        system_info: SystemInfo = None,  # type: ignore
    ):
        self.name = name
        self.type = str(type)
        self.version = version
        self.objs = objs
        # Used for storing custom information other than the version and public_info.
        self.metadata = metadata
        self.public_info = public_info
        self.system_info = system_info

    @staticmethod
    def parse_public_info(dist_data: DistData) -> dict:
        model_meta = DeviceObjectCollection()
        assert dist_data.meta.Unpack(model_meta)
        model_info = json.loads(model_meta.public_info)
        return json.loads(model_info["public_info"])

    def check(
        self,
        model_type: str = None,
        max_version: Version = None,
    ):
        if model_type and model_type != self.type:
            raise ValueError(f"model type mismatch, {model_type}, {self.type}")

        if max_version and not (
            max_version.major == self.version.major
            and max_version.minor >= self.version.minor
        ):
            raise ValueError(f"model version mismatch, {self.version}, {max_version}")

    @staticmethod
    def load(storage: Storage, dist_data: DistData, pyus: dict[str, PYU] | None = None, spu: SPU | Callable[[], SPU] = None) -> 'Model':  # type: ignore
        model_meta = DeviceObjectCollection()
        assert dist_data.meta.Unpack(model_meta)

        model_info = json.loads(model_meta.public_info)

        assert (
            isinstance(model_info, dict)
            and "major_version" in model_info
            and "minor_version" in model_info
            and "public_info" in model_info
        )

        objs = []
        for save_obj in model_meta.objs:
            if save_obj.type == "pyu":
                assert len(save_obj.data_ref_idxs) == 1
                data_ref = dist_data.data_refs[save_obj.data_ref_idxs[0]]
                party = data_ref.party
                if pyus is not None:
                    assert (
                        party in pyus
                    ), f"party {party} not in '{','.join(pyus.keys())}'"
                    pyu = pyus[party]
                else:
                    pyu = PYU(party)

                assert data_ref.format == "pickle"

                def loads(storage: Storage, path: str) -> Any:
                    with storage.get_reader(path) as r:
                        return pickle.load(r)

                objs.append(pyu(loads)(storage, data_ref.uri))
            elif save_obj.type == "spu":
                # TODO: only support one spu for now
                assert spu is not None
                if inspect.isfunction(spu):
                    spu = spu()
                assert len(save_obj.data_ref_idxs) > 1
                full_paths = {}
                for data_ref_idx in save_obj.data_ref_idxs:
                    data_ref = dist_data.data_refs[data_ref_idx]
                    assert data_ref.format == "pickle"
                    party = data_ref.party
                    assert party not in full_paths
                    uri = data_ref.uri
                    full_paths[party] = lambda uri=uri: storage.get_reader(uri)
                assert set(full_paths.keys()) == set(spu.actors.keys())
                spu_paths = [full_paths[party] for party in spu.actors.keys()]
                objs.append(spu.load(spu_paths))
            else:
                raise RuntimeError(f"not supported objs type {save_obj.type}")

        public_info = model_info.pop("public_info")
        major, minor = model_info.pop("major_version"), model_info.pop("minor_version")
        version = Version(major, minor)

        return Model(
            name=dist_data.name,
            type=dist_data.type,
            version=version,
            public_info=public_info,
            metadata=model_info,
            system_info=dist_data.system_info,
            objs=objs,
        )

    def dump(self, storage: Storage, output_uris: str) -> DistData:  # type: ignore
        assert output_uris != "", f"output_uris cannot be empty"
        assert self.name != "", f"name is not set, uri is {output_uris}"
        assert self.type != "", f"type is not set, uri is {output_uris}"

        objs_uri = []
        objs_party = []
        saved_objs = []
        for i, obj in enumerate(self.objs):
            if isinstance(obj, PYUObject):
                device: PYU = obj.device
                uri = f"{output_uris}/{i}"

                def dumps(comp_storage, uri: str, obj: Any):
                    with comp_storage.get_writer(uri) as w:
                        pickle.dump(obj, w)

                wait(device(dumps)(storage, uri, obj))

                saved_obj = DeviceObjectCollection.DeviceObject(
                    type="pyu", data_ref_idxs=[len(objs_uri)]
                )
                saved_objs.append(saved_obj)
                objs_uri.append(uri)
                objs_party.append(device.party)
            elif isinstance(obj, SPUObject):
                device: SPU = obj.device
                uris = [f"{output_uris}/{i}" for _ in device.actors]

                device.dump(
                    obj,
                    [lambda uri=uri: storage.get_writer(uri) for uri in uris],
                )

                saved_obj = DeviceObjectCollection.DeviceObject(
                    type="spu",
                    data_ref_idxs=[len(objs_uri) + p for p in range(len(uris))],
                )
                saved_objs.append(saved_obj)
                objs_uri.extend(uris)
                objs_party.extend(list(device.actors.keys()))
            else:
                raise RuntimeError(f"not supported objs type {type(obj)}")

        model_info = {
            "major_version": self.version.major,
            "minor_version": self.version.minor,
            "public_info": self.public_info,
        }
        if self.metadata:
            for k, v in self.metadata.items():
                model_info[k] = v

        meta = DeviceObjectCollection(
            objs=saved_objs,
            public_info=json.dumps(model_info),
        )

        dd = DistData(
            name=self.name,
            type=str(self.type),
            system_info=self.system_info,
            data_refs=[
                DistData.DataRef(uri=uri, party=p, format="pickle")
                for uri, p in zip(objs_uri, objs_party)
            ],
        )
        dd.meta.Pack(meta)
        return dd
