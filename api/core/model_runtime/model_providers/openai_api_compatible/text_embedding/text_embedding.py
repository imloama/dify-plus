import json
import time
from decimal import Decimal
from typing import Optional
from urllib.parse import urljoin

import numpy as np
import requests

from core.entities.embedding_type import EmbeddingInputType
from core.model_runtime.entities.common_entities import I18nObject
from core.model_runtime.entities.model_entities import (
    AIModelEntity,
    FetchFrom,
    ModelPropertyKey,
    ModelType,
    PriceConfig,
    PriceType,
)
from core.model_runtime.entities.text_embedding_entities import EmbeddingUsage, TextEmbeddingResult
from core.model_runtime.errors.validate import CredentialsValidateFailedError
from core.model_runtime.model_providers.__base.text_embedding_model import TextEmbeddingModel
from core.model_runtime.model_providers.openai_api_compatible._common import _CommonOaiApiCompat
from core.model_runtime.entities.llm_entities import LLMUsage # Extend: Supplier model billing logic modification


class OAICompatEmbeddingModel(_CommonOaiApiCompat, TextEmbeddingModel):
    """
    Model class for an OpenAI API-compatible text embedding model.
    """

    def _invoke(
        self,
        model: str,
        credentials: dict,
        texts: list[str],
        user: Optional[str] = None,
        input_type: EmbeddingInputType = EmbeddingInputType.DOCUMENT,
    ) -> TextEmbeddingResult:
        """
        Invoke text embedding model

        :param model: model name
        :param credentials: model credentials
        :param texts: texts to embed
        :param user: unique user id
        :param input_type: input type
        :return: embeddings result
        """

        # Prepare headers and payload for the request
        headers = {"Content-Type": "application/json"}

        api_key = credentials.get("api_key")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        endpoint_url = credentials.get("endpoint_url")
        assert endpoint_url is not None, "endpoint_url is required in credentials"
        if not endpoint_url.endswith("/"):
            endpoint_url += "/"

        endpoint_url = urljoin(endpoint_url, "embeddings")

        extra_model_kwargs = {}
        if user:
            extra_model_kwargs["user"] = user

        extra_model_kwargs["encoding_format"] = "float"

        # get model properties
        context_size = self._get_context_size(model, credentials)
        max_chunks = self._get_max_chunks(model, credentials)

        inputs = []
        indices = []
        used_tokens = 0
        completion_tokens = 0  # Extend: Supplier model billing logic modification

        for i, text in enumerate(texts):
            # Here token count is only an approximation based on the GPT2 tokenizer
            # TODO: Optimize for better token estimation and chunking
            num_tokens = self._get_num_tokens_by_gpt2(text)

            if num_tokens >= context_size:
                cutoff = int(np.floor(len(text) * (context_size / num_tokens)))
                # if num tokens is larger than context length, only use the start
                inputs.append(text[0:cutoff])
            else:
                inputs.append(text)
            indices += [i]

        batched_embeddings = []
        _iter = range(0, len(inputs), max_chunks)

        for i in _iter:
            # Prepare the payload for the request
            payload = {"input": inputs[i : i + max_chunks], "model": model, **extra_model_kwargs}

            # Make the request to the OpenAI API
            response = requests.post(endpoint_url, headers=headers, data=json.dumps(payload), timeout=(10, 300))

            response.raise_for_status()  # Raise an exception for HTTP errors
            response_data = response.json()

            # Extract embeddings and used tokens from the response
            embeddings_batch = [data["embedding"] for data in response_data["data"]]
            completion_used_tokens = response_data["usage"]["completion_tokens"] # Extend: Supplier model billing logic modification
            embedding_used_tokens = response_data["usage"]["total_tokens"]

            used_tokens += embedding_used_tokens
            batched_embeddings += embeddings_batch
            completion_tokens += completion_used_tokens # Extend: Supplier model billing logic modification

        # calc usage
        usage = self._calc_response_usage(
                    model=model, credentials=credentials, tokens=used_tokens, completion=completion_tokens) # Extend: Supplier model billing logic modification

        return TextEmbeddingResult(embeddings=batched_embeddings, usage=usage, model=model)

    def get_num_tokens(self, model: str, credentials: dict, texts: list[str]) -> int:
        """
        Approximate number of tokens for given messages using GPT2 tokenizer

        :param model: model name
        :param credentials: model credentials
        :param texts: texts to embed
        :return:
        """
        return sum(self._get_num_tokens_by_gpt2(text) for text in texts)

    def validate_credentials(self, model: str, credentials: dict) -> None:
        """
        Validate model credentials

        :param model: model name
        :param credentials: model credentials
        :return:
        """
        try:
            headers = {"Content-Type": "application/json"}

            api_key = credentials.get("api_key")

            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

            endpoint_url = credentials.get("endpoint_url", "")
            if not endpoint_url.endswith("/"):
                endpoint_url += "/"

            endpoint_url = urljoin(endpoint_url, "embeddings")

            payload = {"input": "ping", "model": model}
            # For nvidia models, the "input_type":"query" need in the payload
            # more to check issue #11193 or NvidiaTextEmbeddingModel
            if model.startswith("nvidia/"):
                payload["input_type"] = "query"

            response = requests.post(url=endpoint_url, headers=headers, data=json.dumps(payload), timeout=(10, 300))

            if response.status_code != 200:
                raise CredentialsValidateFailedError(
                    f"Credentials validation failed with status code {response.status_code}"
                )

            try:
                json_result = response.json()
            except json.JSONDecodeError as e:
                raise CredentialsValidateFailedError("Credentials validation failed: JSON decode error")

            if "model" not in json_result:
                raise CredentialsValidateFailedError("Credentials validation failed: invalid response")
        except CredentialsValidateFailedError:
            raise
        except Exception as ex:
            raise CredentialsValidateFailedError(str(ex))

    def get_customizable_model_schema(self, model: str, credentials: dict) -> AIModelEntity:
        """
        generate custom model entities from credentials
        """
        entity = AIModelEntity(
            model=model,
            label=I18nObject(en_US=model),
            model_type=ModelType.TEXT_EMBEDDING,
            fetch_from=FetchFrom.CUSTOMIZABLE_MODEL,
            model_properties={
                ModelPropertyKey.CONTEXT_SIZE: int(credentials.get("context_size", 512)),
                ModelPropertyKey.MAX_CHUNKS: 1,
            },
            parameter_rules=[],
            pricing=PriceConfig(
                input=Decimal(credentials.get("input_price", 0)),
                unit=Decimal(credentials.get("unit", 0)),
                currency=credentials.get("currency", "USD"),
            ),
        )

        return entity

    def _calc_response_usage(self, model: str, credentials: dict, tokens: int, completion: int) -> LLMUsage:
        """
        Calculate response usage

        :param model: model name
        :param credentials: model credentials
        :param tokens: input tokens
        :return: usage
        """
        # get input price info
        input_price_info = self.get_price(
            model=model, credentials=credentials, price_type=PriceType.INPUT, tokens=tokens
        )

        # Extend: Start Supplier model billing logic modification

        # get out price info
        completion_price_info = self.get_price(
            model=model, credentials=credentials, price_type=PriceType.OUTPUT, tokens=completion
        )

        # transform usage
        usage = LLMUsage(
            tokens=tokens,
            completion_tokens=completion,
            total_tokens=tokens + completion,
            prompt_unit_price=input_price_info.unit_price,
            prompt_price_unit=input_price_info.unit,
            prompt_price=input_price_info.total_amount,
            completion_unit_price=completion_price_info.unit_price,
            completion_price_unit=completion_price_info.unit,
            completion_price=completion_price_info.total_amount,
            total_price=input_price_info.total_amount + completion_price_info.total_amount,
            currency=input_price_info.currency,
            latency=time.perf_counter() - self.started_at,
        )
        # Extend: Stop Supplier model billing logic modification

        return usage
