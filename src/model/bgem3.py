from typing import Dict, Optional, Union, List

from ..utils.arguments import ModelArguments
from ..utils.info_nce import InfoNCE

import torch
from torch import nn, Tensor
import torch.nn.functional as F
from transformers import AutoModel, AutoTokenizer, XLMRobertaModel, XLMRobertaTokenizer


class M3DenseEmbedModel(nn.Module):

    def __init__(self, model_args: ModelArguments):
        super().__init__()
        self.load_model(model_args)
        self.vocab_size = self.model.config.vocab_size
        self.cross_entropy = nn.CrossEntropyLoss(reduction='mean')
        self.info_nce = InfoNCE(negative_mode="paired")

        self.normlized = model_args.normlized
        self.temperature = model_args.temperature
        self.sub_batch_size = model_args.encode_sub_batch_size

        if not model_args.normlized:
            self.temperature = 1.0

    def gradient_checkpointing_enable(self, **kwargs):
        self.model.enable_input_require_grads()
        self.model.gradient_checkpointing_enable(**kwargs)

    def load_model(self, model_load_args:ModelArguments):
        if model_load_args.train_with_qlora:
            self.model: XLMRobertaModel = AutoModel.from_pretrained(
                model_load_args.model_path,
                low_cpu_mem_usage=True,
                torch_dtype=torch.bfloat16,
                device_map="auto",
                load_in_4bit=True,
                bnb_4bit_compute_dtype=torch.bfloat16,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True,
            )
        else:
            torch_dtype = torch.float32
            if model_load_args.model_with_fp16:
                torch_dtype = torch.half
            self.model: XLMRobertaModel = AutoModel.from_pretrained(
                model_load_args.model_path,
                low_cpu_mem_usage=True,
                device_map="auto",
                torch_dtype=torch_dtype,
                add_pooling_layer=False,
            )

        self.tokenizer:XLMRobertaTokenizer = AutoTokenizer.from_pretrained(model_load_args.tokenizer_path)

        if model_load_args.train_with_lora:
            from peft import LoraConfig, TaskType, get_peft_model
            config = LoraConfig(task_type=TaskType.FEATURE_EXTRACTION, target_modules=model_load_args.lora_modules)
            self.model = get_peft_model(self.model, config)
            if model_load_args.lora_with_fp16:
                for name, param in self.model.named_parameters():
                    if "lora" in name:
                        param.data = param.data.half()
                        if param.grad is not None and param.grad.data is not None:
                            param.grad.data = param.grad.data.half()

    def dense_score(self, q_reps, p_reps):
        scores = self.compute_similarity(q_reps, p_reps) / self.temperature
        scores = scores.view(q_reps.size(0), -1)
        return scores

    def _encode(self, features) -> Tensor:
        dense_vecs = None
        last_hidden_state = self.model(**features, return_dict=True).last_hidden_state
        dense_vecs = last_hidden_state[:, 0]
        if self.normlized:
            dense_vecs = F.normalize(dense_vecs, dim=-1)
        return dense_vecs

    def encode(self, features: Dict[str, Tensor]=None) -> Tensor:
        if features is None:
            return None

        if self.sub_batch_size is not None and self.sub_batch_size != -1:
            all_dense_vecs = []
            for i in range(0, len(features['attention_mask']), self.sub_batch_size):
                end_inx = min(i + self.sub_batch_size, len(features['attention_mask']))
                sub_features = {}
                for k, v in features.items():
                    sub_features[k] = v[i:end_inx]
                all_dense_vecs.append(self._encode(sub_features))
            dense_vecs = torch.cat(all_dense_vecs, 0)
        else:
            dense_vecs = self._encode(features)

        return dense_vecs.contiguous()

    def compute_similarity(self, q_reps:Tensor, p_reps:Tensor):
        if len(p_reps.size()) == 2:
            return torch.matmul(q_reps, p_reps.transpose(0, 1))
        return torch.matmul(q_reps, p_reps.transpose(-2, -1))

    def entity_reconstruction_loss(self, q_dense_vecs: Tensor, p_dense_vecs: Tensor):
        idxs = torch.arange(q_dense_vecs.size(0), device=q_dense_vecs.device, dtype=torch.long)

        targets = idxs * (p_dense_vecs.size(0) // q_dense_vecs.size(0))
        dense_scores = self.dense_score(q_dense_vecs, p_dense_vecs)  # B, B * N
        loss = self.cross_entropy(dense_scores, targets)

        return loss

    def kg_embed_loss(self, head, link_desc, tail) -> List[Tensor]:
        head_pos = head[:, 0, :]
        head_neg = head[:, 1:, :]
        tail_pos = tail[:, 0, :]
        tail_neg = tail[:, 1:, :]

        return self.info_nce(head_pos + link_desc, tail_pos, tail_neg), self.info_nce(tail_pos - link_desc, head_pos, head_neg)

    def forward(self, inputs):
        # torch.cuda.empty_cache()
        head, head_desc, link_desc, tail, tail_desc = inputs

        # (batch_size, group_size, embed_size)
        head, head_desc, tail, tail_desc = map(lambda x: torch.stack(list(map(self.encode, x)), dim=1), (head, head_desc, tail, tail_desc))
        # (batch_size, embed_size)
        link_desc = self.encode(link_desc)

        query = torch.cat([head[:, 0, :], tail[:, 0, :]], dim=0)
        passage = torch.cat([head_desc, tail_desc], dim=0)

        loss1 = self.entity_reconstruction_loss(query, passage)
        loss2, loss3 = self.kg_embed_loss(head, link_desc, tail)

        return (loss1 + loss2 + loss3) / 3

    def save(self, output_dir: str) -> None:
        _trans_state_dict = lambda state_dict: type(state_dict)({k: v.clone().cpu() for k,v in state_dict.items()})
        self.model.save_pretrained(output_dir, state_dict=_trans_state_dict(self.model.state_dict()))


class M3ForInference(M3DenseEmbedModel):
    def __init__(
        self,
        model_load_args: ModelArguments = None,
        normlized: bool = True,
        sentence_pooling_method: str = "cls",
        temperature: float = 1.0,
        use_fp16: bool = True,
        device: str = "cpu"
    ):
        super().__init__(
            model_load_args=model_load_args,
            normlized=normlized,
            sentence_pooling_method=sentence_pooling_method,
            temperature=temperature,
            enable_sub_batch=False,
        )
        if torch.cuda.is_available() and device == "cuda":
            self.device = torch.device("cuda")
        else:
            self.device = torch.device("cpu")
            use_fp16 = False
        if use_fp16: self.model.half()
        self.model = self.model.to(self.device)
        self.num_gpus = torch.cuda.device_count()
        if self.num_gpus > 1:
            self.model = torch.nn.DataParallel(self.model)

    def __call__(
        self,
        sentences: Union[List[str], str],
        batch_size: int = 256,
        max_length: int = 8192,
    ) -> Tensor:
        self.model.eval()

        input_was_string = False
        if isinstance(sentences, str):
            sentences = [sentences]
            batch_size = None
            input_was_string = True

        inputs = self.tokenizer(
            sentences,
            padding=True,
            truncation=True,
            return_tensors="pt",
            max_length=max_length,
        ).to(self.device)
        all_embeddings = self.encode(inputs, batch_size)

        if input_was_string:
            return all_embeddings[0]
        return all_embeddings


class M3ForScore(M3DenseEmbedModel):
    def __init__(
        self,
        model_load_args: ModelArguments,
        normlized: bool = True,
        sentence_pooling_method: str = "cls",
        temperature: float = 1.0,
        use_fp16: bool = True,
        device: str = "cpu",
        batch_size: int = 512
    ):
        super().__init__(
            model_load_args=model_load_args,
            normlized=normlized,
            sentence_pooling_method=sentence_pooling_method,
            temperature=temperature,
            enable_sub_batch=False,
        )
        if torch.cuda.is_available() and device == "cuda":
            self.device = torch.device("cuda")
        else:
            self.device = torch.device("cpu")
            use_fp16 = False
        if use_fp16: self.model.half()
        self.model = self.model.to(self.device)
        self.model.eval()
        self.num_gpus = torch.cuda.device_count()
        if self.num_gpus > 1:
            self.model = torch.nn.DataParallel(self.model)
        self.batch_size = batch_size
        self.max_length = 8192

    def select_topk(self, query: str, documents: List[str], k=1) -> torch.Tensor:
        """
        Returns:
            `ret`: `torch.return_types.topk`, use `ret.values` or `ret.indices` to get value or index tensor
        """
        query = self.tokenizer(
            [query],
            padding=True,
            truncation=True,
            return_tensors="pt",
            max_length=self.max_length,
        ).to(self.device)
        documents = self.tokenizer(
            documents,
            padding=True,
            truncation=True,
            return_tensors="pt",
            max_length=self.max_length,
        ).to(self.device)

        query = self.encode(query)
        documents = self.encode(documents, self.batch_size)

        scores = self.dense_score(query, documents).squeeze_()
        return scores.topk(min(k, len(scores))).indices

    def __call__(self, query, paragraphs: List[Dict[str, str]], topk=5) -> List[Dict[str, str]]:
        torch.cuda.empty_cache()
        texts = [item['text'] for item in paragraphs]
        topk = self.select_topk(query, texts, topk)
        indices = topk.detach().cpu().numpy().tolist()
        return [paragraphs[int(idx)] for idx in indices]
