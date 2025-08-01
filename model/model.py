'''
    code by TaeHwan Jung(@graykode)
    Original Paper and repository here : https://github.com/openai/gpt-2
    GPT2 Pytorch Model : https://github.com/huggingface/pytorch-pretrained-BERT
'''

import copy
import torch
import math
import torch.nn as nn
from torch.nn.parameter import Parameter

class LayerNorm(nn.Module):
  def __init__(self, hidden_size, eps=1e-12):
    super().__init__()
    self.weight = nn.Parameter(torch.ones(hidden_size))
    self.bias = nn.Parameter(torch.zeros(hidden_size))
    self.variance_epsilon = eps

  def forward(self, x):
    u = x.mean(dim=-1, keepdim=True)
    s = x.var(dim=-1, keepdim=True, unbiased=False)
    norm_x = (x - u) / torch.sqrt(s + self.variance_epsilon)
    return self.weight * norm_x + self.bias

class Conv1D(nn.Module):
  def __init__(self, nf, nx):
    super().__init__()
    self.nf = nf
    w = torch.empty(nx, nf)
    nn.init.normal_(w, std=0.02)
    self.weight = Parameter(w)
    self.bias = Parameter(torch.zeros(nf))

  def forward(self, x):
    size_out = x.size()[:-1] + (self.nf,)
    x = torch.addmm(self.bias, x.view(-1, x.size(-1)), self.weight)
    x = x.view(*size_out)
    return x

class Attention(nn.Module):
  def __init__(self, nx, n_ctx, config, scale=False):
    super().__init__()
    n_state = nx
    assert n_state % config.n_head == 0
    self.head_features = n_state // config.n_head
    self.n_head = config.n_head
    self.n_state = n_state
    self.scale = scale
    self.c_attn = Conv1D(n_state * 3, nx)
    self.c_proj = Conv1D(n_state, nx)
    self.register_buffer("bias", torch.tril(torch.ones(n_ctx, n_ctx)).view(1, 1, n_ctx, n_ctx))

  def _attn(self, q, k, v):
    w = q @ k
    if self.scale:
      w = w / math.sqrt(v.size(-1))
    nd, ns = w.size(-2), w.size(-1)
    b = self.bias[:, :, ns-nd:ns, :ns]
    w = w * b - 1e10 * (1 - b)
    w = torch.softmax(w, dim=-1)
    return w @ v

  def forward(self, x, layer_past=None):
    x = self.c_attn(x)

    query, key, value = x.split(self.n_state, dim=2)
    query = query.view(
              *query.size()[:-1], self.n_head, self.head_features
            ).transpose(1, 2)     # (batch, head, seq_length, head_features)
    key   = key.view(
              *key.size()[:-1], self.n_head, self.head_features
            ).permute(0, 2, 3, 1) # (batch, head, head_features, seq_length)
    value = value.view(
              *value.size()[:-1], self.n_head, self.head_features
            ).transpose(1, 2)     # (batch, head, seq_length, head_features)

    if layer_past is not None:
      past_key, past_value = layer_past[0].transpose(-2, -1), layer_past[1]  # transpose back cf below
      key = torch.cat((past_key, key), dim=-1)
      value = torch.cat((past_value, value), dim=-2)

    present = torch.stack((key.transpose(2, 3), value))  # transpose to have same shapes for stacking

    a = self._attn(query, key, value)

    a = a.transpose(1, 2).contiguous()
    a = a.view(*a.size()[:-2], self.n_state)

    a = self.c_proj(a)

    return a, present

class MLP(nn.Module):
  def __init__(self, n_state, config):  # in MLP: n_state=3072 (4 * n_embd)
    super().__init__()
    nx = config.n_embd
    self.c_fc = Conv1D(n_state, nx)
    self.c_proj = Conv1D(nx, n_state)
    self.act = nn.GELU()

  def forward(self, x):
    h = self.act(self.c_fc(x))
    h2 = self.c_proj(h)
    return h2

class Block(nn.Module):
  def __init__(self, n_ctx, config, scale=False):
    super().__init__()
    nx = config.n_embd
    self.ln_1 = LayerNorm(nx, eps=config.layer_norm_epsilon)
    self.attn = Attention(nx, n_ctx, config, scale)
    self.ln_2 = LayerNorm(nx, eps=config.layer_norm_epsilon)
    self.mlp = MLP(4 * nx, config)

  def forward(self, x, layer_past=None):
    a, present = self.attn(self.ln_1(x), layer_past=layer_past)
    x = x + a # shortcut connection
    m = self.mlp(self.ln_2(x))
    x = x + m # shortcut connection
    return x, present

class GPT2Model(nn.Module):
  def __init__(self, config):
    super().__init__()
    self.n_layer = config.n_layer
    self.n_embd = config.n_embd
    self.n_vocab = config.vocab_size

    self.wte = nn.Embedding(config.vocab_size, config.n_embd)
    self.wpe = nn.Embedding(config.n_positions, config.n_embd)
    block = Block(config.n_ctx, config, scale=True)
    self.h = nn.ModuleList([copy.deepcopy(block) for _ in range(config.n_layer)])
    self.ln_f = LayerNorm(config.n_embd, eps=config.layer_norm_epsilon)

  def set_embeddings_weights(self, model_embeddings_weights):
    embed_shape = model_embeddings_weights.shape
    self.decoder = nn.Linear(embed_shape[1], embed_shape[0], bias=False)
    self.decoder.weight = model_embeddings_weights  # Tied weights

  def forward(self, input_ids, position_ids=None, token_type_ids=None, past=None):
    if past is None:
      past_length = 0
      past = [None] * len(self.h)
    else:
      past_length = past[0][0].size(-2)
    if position_ids is None:
      position_ids = torch.arange(past_length, input_ids.size(-1) + past_length, dtype=torch.long, device=input_ids.device)
      position_ids = position_ids.unsqueeze(0).expand_as(input_ids)

    input_shape = input_ids.size()
    input_ids = input_ids.view(-1, input_ids.size(-1))
    position_ids = position_ids.view(-1, position_ids.size(-1))

    inputs_embeds = self.wte(input_ids)
    position_embeds = self.wpe(position_ids)
    if token_type_ids is not None:
      token_type_ids = token_type_ids.view(-1, token_type_ids.size(-1))
      token_type_embeds = self.wte(token_type_ids)
    else:
      token_type_embeds = 0
    hidden_states = inputs_embeds + position_embeds + token_type_embeds
    presents = []
    for block, layer_past in zip(self.h, past):
      hidden_states, present = block(hidden_states, layer_past)
      presents.append(present)
    hidden_states = self.ln_f(hidden_states)
    output_shape = input_shape + (hidden_states.size(-1),)
    return hidden_states.view(*output_shape), presents

class GPT2LMHead(nn.Module):
  def __init__(self, model_embeddings_weights, config):
    super().__init__()
    self.n_embd = config.n_embd
    self.set_embeddings_weights(model_embeddings_weights)

  def set_embeddings_weights(self, model_embeddings_weights):
    embed_shape = model_embeddings_weights.shape
    self.decoder = nn.Linear(embed_shape[1], embed_shape[0], bias=False)
    self.decoder.weight = model_embeddings_weights  # Tied weights

  def forward(self, hidden_state):
    lm_logits = self.decoder(hidden_state)
    return lm_logits

class GPT2LMHeadModel(nn.Module):
  def __init__(self, config):
    super().__init__()
    self.transformer = GPT2Model(config)
    self.lm_head = GPT2LMHead(self.transformer.wte.weight, config)

  def set_tied(self):
    self.lm_head.set_embeddings_weights(self.transformer.wte.weight)

  def forward(self, input_ids, position_ids=None, token_type_ids=None, lm_labels=None, past=None):
    hidden_states, presents = self.transformer(input_ids, position_ids, token_type_ids, past)
    lm_logits = self.lm_head(hidden_states)
    if lm_labels is not None:
      loss_fct = nn.CrossEntropyLoss(ignore_index=-1)
      loss = loss_fct(lm_logits.view(-1, lm_logits.size(-1)), lm_labels.view(-1))
      return loss
    return lm_logits, presents