"""
In this mixin, I use a different implementation than lora.py
I just use a fake linear layer to replace any model with lora mixin.
"""

import torch
import torch.nn as nn
from sat.model.base_model import BaseMixin
import math
from sat.helpers import print_all
from sat.model.transformer import RowParallelLinear, ColumnParallelLinear

class HackLinear(nn.Linear):
    def _load_from_state_dict(self, state_dict, prefix, local_metadata, strict, missing_keys, unexpected_keys, error_msgs):
        if prefix + 'weight' in state_dict:
            self.weight.data.copy_(state_dict[prefix+'weight'])
        if prefix + 'bias' in state_dict:
            self.bias.data.copy_(state_dict[prefix+'bias'])

try:
    from bitsandbytes.nn import LinearNF4
    def copy_nested_list(src, dst):
        for i in range(len(dst)):
            if type(dst[i]) is torch.Tensor:
                dst[i].copy_(src[i])
            elif type(dst[i]) is list:
                copy_nested_list(src[i], dst[i])
            else:
                dst[i] = src[i]
    class HackLinearNF4(LinearNF4):
        def _load_from_state_dict(self, state_dict, prefix, local_metadata, strict, missing_keys, unexpected_keys, error_msgs):
            if prefix + 'weight' in state_dict:
                self.weight.data.copy_(state_dict[prefix+'weight'])
                if self.weight.data.dtype == torch.uint8:
                    copy_nested_list(state_dict[prefix+'quant_state'], self.weight.quant_state)
            if prefix + 'bias' in state_dict:
                self.bias.data.copy_(state_dict[prefix+'bias'])
        def _save_to_state_dict(self, destination, prefix, keep_vars):
            super()._save_to_state_dict(destination, prefix, keep_vars)
            destination[prefix+'quant_state'] = self.weight.quant_state
except Exception as exception:
    print_all("Failed to load bitsandbytes:" + str(exception), level='WARNING')


class HackParameterList(nn.ParameterList):
    def _load_from_state_dict(self, state_dict, prefix, local_metadata, strict, missing_keys, unexpected_keys, error_msgs):
        for i in range(len(self)):
            if prefix + str(i) in state_dict:
                self[i].data.copy_(state_dict[prefix+str(i)])

class LoraLinear(nn.Module):
    def __init__(self, in_dim, out_dim, r, lora_alpha=1., lora_dropout=0., qlora=False):
        super().__init__()
        if lora_dropout and lora_dropout > 0:
            self.lora_dropout = nn.Dropout(p=lora_dropout)
        else:
            self.lora_dropout = lambda x: x
        self.r = r
        self.lora_alpha = lora_alpha
        self.scaling = self.lora_alpha / self.r
        if qlora:
            self.original = HackLinearNF4(in_dim, out_dim)
        else:
            self.original = HackLinear(in_dim, out_dim)
        self.matrix_A = nn.Parameter(torch.empty((r, in_dim)))
        self.matrix_B = nn.Parameter(torch.empty((out_dim, r)))
        nn.init.kaiming_uniform_(self.matrix_A, a=math.sqrt(5))
        nn.init.zeros_(self.matrix_B)

    def _load_from_state_dict(self, state_dict, prefix, local_metadata, strict, missing_keys, unexpected_keys, error_msgs):
        # This is not a perfect version, becuase it doesn't handle errors and unexpected keys.
        if prefix + 'weight' in state_dict:
            # load from normal Linear
            self.original._load_from_state_dict(state_dict, prefix, local_metadata, strict, missing_keys, unexpected_keys, error_msgs)
        else:
            # load from LoraLinear
            super()._load_from_state_dict(state_dict, prefix, local_metadata, strict, missing_keys, unexpected_keys, error_msgs)
            
    def forward(self, x):
        return self.original(x) + (self.lora_dropout(x) @ self.matrix_A.T @ self.matrix_B.T) * self.scaling


class LoraQKV(nn.Module):
    def __init__(self, in_dim, out_dim, r, lora_alpha=1., lora_dropout=0., head_first=False, num_attention_heads=None, hidden_size_per_attention_head=None, qlora=False):
        """
        You can use safely with this layer, ONLY WHEN query_key_value output is query_key_value order.
        If you use a different order like ChatGLM
        """
        super().__init__()
        if lora_dropout and lora_dropout > 0:
            self.lora_dropout = nn.Dropout(p=lora_dropout)
        else:
            self.lora_dropout = lambda x: x
        self.r = r
        self.lora_alpha = lora_alpha
        self.scaling = self.lora_alpha / self.r
        if qlora:
            self.original = HackLinearNF4(in_dim, out_dim)
        else:
            self.original = HackLinear(in_dim, out_dim)
        self.matrix_A = HackParameterList([nn.Parameter(torch.empty((r, in_dim))) for _ in range(3)])
        self.matrix_B = HackParameterList([nn.Parameter(torch.empty((out_dim // 3, r))) for _ in range(3)])
        for i in range(3):
            nn.init.kaiming_uniform_(self.matrix_A[i], a=math.sqrt(5))
            nn.init.zeros_(self.matrix_B[i])
        self.head_first = head_first
        if head_first:
            assert num_attention_heads is not None and hidden_size_per_attention_head is not None, "You should set num_attention_heads and hidden_size_per_attention_head if you use head_first=True!"
            self.num_attention_heads = num_attention_heads
            self.hidden_size_per_attention_head = hidden_size_per_attention_head

    def _load_from_state_dict(self, state_dict, prefix, local_metadata, strict, missing_keys, unexpected_keys, error_msgs):
        # This is not a perfect version, becuase it doesn't handle errors and unexpected keys.
        if prefix + 'weight' in state_dict:
            # load from normal Linear
            self.original._load_from_state_dict(state_dict, prefix, local_metadata, strict, missing_keys, unexpected_keys, error_msgs)
        else:
            # load from LoraLinear
            super()._load_from_state_dict(state_dict, prefix, local_metadata, strict, missing_keys, unexpected_keys, error_msgs)
            
    def forward(self, x):
        mixed_raw_layer = self.original(x)
        lora_outputs = []
        for i in range(3):
            lora_outputs.append((self.lora_dropout(x) @ self.matrix_A[i].T @ self.matrix_B[i].T) * self.scaling)
        if self.head_first:
            new_tensor_shape = lora_outputs[0].size()[:-1] + (
                self.num_attention_heads,
                self.hidden_size_per_attention_head,
            )
            for i in range(3):
                lora_outputs[i] = lora_outputs[i].view(*new_tensor_shape)
            mixed_raw_layer = mixed_raw_layer + torch.cat(lora_outputs, -1).view(*mixed_raw_layer.size())
        else:
            mixed_raw_layer = mixed_raw_layer + torch.cat(lora_outputs, -1)

        return mixed_raw_layer


def replace_linear_with_lora(lin, base_cls, r, *args, **kw_args):
    # not supported for linear without bias for now
    out_dim, in_dim = lin.weight.shape
    return base_cls(in_dim, out_dim, r, *args, **kw_args)

def merge_linear_lora(lin):
    out_dim, in_dim = lin.original.weight.shape
    new_lin = nn.Linear(in_dim, out_dim)
    new_lin.bias.data = lin.original.bias.data
    new_lin.weight.data = lin.original.weight.data + (lin.matrix_A.data.T.float() @ lin.matrix_B.data.T.float() * lin.scaling).T.to(lin.original.weight.data.dtype)
    return new_lin

def merge_qkv_lora(lin):
    out_dim, in_dim = lin.original.weight.shape
    new_lin = nn.Linear(in_dim, out_dim)
    new_lin.bias.data = lin.original.bias.data
    new_qkv = []
    for i in range(3):
        new_qkv.append(lin.matrix_A[i].data.T.float() @ lin.matrix_B[i].data.T.float() * lin.scaling)
    if lin.head_first:
        ini_shape = new_qkv[0].shape
        new_qkv = [x.view(ini_shape[0], lin.num_attention_heads, -1) for x in new_qkv]
        new_qkv = torch.cat(new_qkv, -1).view(ini_shape[0], 3*ini_shape[1])
    else:
        new_qkv = torch.cat(new_qkv, -1)
    new_lin.weight.data = lin.original.weight.data + new_qkv.T.to(lin.original.weight.data.dtype)
    return new_lin

class LoraMixin(BaseMixin):
    def __init__(self, 
                layer_num,
                r: int = 0,
                lora_alpha: int = 1,
                lora_dropout: float = 0.,
                layer_range = None,
                head_first = False,
                num_attention_heads = None,
                hidden_size_per_attention_head = None,
                qlora = False):
        super().__init__()
        self.r = r
        self.lora_alpha = lora_alpha
        self.lora_dropout = lora_dropout

        if layer_range is None:
            layer_range = [i for i in range(layer_num)]
        self.layer_range = layer_range

        self.scaling = self.lora_alpha / self.r
        self.head_first = head_first
        self.num_attention_heads = num_attention_heads
        self.hidden_size_per_attention_head = hidden_size_per_attention_head
        self.qlora = qlora

    def reinit(self, parent_model):
        """
        only support self-attention part
        not supported for cross-attention for now
        """
        for i in self.layer_range:
            print(f'replacing layer {i} with lora')
            parent_model.transformer.layers[i].attention.dense = replace_linear_with_lora(parent_model.transformer.layers[i].attention.dense, LoraLinear, self.r, self.lora_alpha, self.lora_dropout, self.qlora)
            parent_model.transformer.layers[i].attention.query_key_value = replace_linear_with_lora(parent_model.transformer.layers[i].attention.query_key_value, LoraQKV, self.r, self.lora_alpha, self.lora_dropout, head_first=self.head_first, num_attention_heads=self.num_attention_heads, hidden_size_per_attention_head=self.hidden_size_per_attention_head, qlora=self.qlora)
        if self.qlora:
            print('replacing chatglm linear layer with 4bit')
            def replace_linear_with_nf4(model, name=None, cache={}):
                if type(model) in (nn.Linear, RowParallelLinear, ColumnParallelLinear):
                    out_dim, in_dim = model.weight.shape
                    return HackLinearNF4(in_dim, out_dim)
                names = set()
                for name, child in model.named_children():
                    if name not in names:
                        if child in cache:
                            new_child = cache[child]
                        else:
                            new_child = replace_linear_with_nf4(child, name=name, cache=cache)
                            cache[child] = new_child
                        setattr(model, name, new_child)
                        names.add(name)
                flag = True
                while flag:
                    flag = False
                    for name, child in model.named_children():
                        if name not in names:
                            setattr(model, name, cache[child])
                            names.add(name)
                            flag = True
                return model
            replace_linear_with_nf4(parent_model.transformer, None, {})

    def merge_lora(self):
        for i in self.layer_range:
            print(f'merge layer {i} lora back to linear')
            self.transformer.layers[i].attention.dense = merge_linear_lora(self.transformer.layers[i].attention.dense)
            self.transformer.layers[i].attention.query_key_value = merge_qkv_lora(self.transformer.layers[i].attention.query_key_value)

if __name__ == '__main__':
    class Model(nn.Module):
        def __init__(self):
            super().__init__()
            self.child = nn.Linear(100, 200)
        
        def forward(self, x):
            return self.child(x)

    model = Model()
    torch.save(model.state_dict(), "linear.pt")
    x = torch.randn(2, 100)
    out1 = model(x)
    model.child = LoraLinear(100, 200, 10)
    model.load_state_dict(torch.load("linear.pt"), strict=False)
    out2 = model(x)
    torch.save(model.state_dict(), "lora.pt")
    ckpt = torch.load("lora.pt")
    breakpoint()
    model.load_state_dict(ckpt, strict=False)
    out3 = model(x)
    breakpoint()