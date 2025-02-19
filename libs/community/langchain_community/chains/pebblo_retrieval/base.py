"""
Pebblo Retrieval Chain with Identity & Semantic Enforcement for question-answering
against a vector database.
"""

import datetime
import inspect
import logging
from http import HTTPStatus
from typing import Any, Dict, List, Optional

import requests  # type: ignore
from langchain.chains.base import Chain
from langchain.chains.combine_documents.base import BaseCombineDocumentsChain
from langchain_core.callbacks import (
    AsyncCallbackManagerForChainRun,
    CallbackManagerForChainRun,
)
from langchain_core.documents import Document
from langchain_core.language_models import BaseLanguageModel
from langchain_core.pydantic_v1 import Extra, Field, validator
from langchain_core.vectorstores import VectorStoreRetriever

from langchain_community.chains.pebblo_retrieval.enforcement_filters import (
    SUPPORTED_VECTORSTORES,
    set_enforcement_filters,
)
from langchain_community.chains.pebblo_retrieval.models import (
    App,
    AuthContext,
    Qa,
    SemanticContext,
)
from langchain_community.chains.pebblo_retrieval.utilities import (
    APP_DISCOVER_URL,
    CLASSIFIER_URL,
    PEBBLO_CLOUD_URL,
    PLUGIN_VERSION,
    PROMPT_URL,
    get_runtime,
)

logger = logging.getLogger(__name__)


class PebbloRetrievalQA(Chain):
    """
    Retrieval Chain with Identity & Semantic Enforcement for question-answering
    against a vector database.
    """

    combine_documents_chain: BaseCombineDocumentsChain
    """Chain to use to combine the documents."""
    input_key: str = "query"  #: :meta private:
    output_key: str = "result"  #: :meta private:
    return_source_documents: bool = False
    """Return the source documents or not."""

    retriever: VectorStoreRetriever = Field(exclude=True)
    """VectorStore to use for retrieval."""
    auth_context_key: str = "auth_context"  #: :meta private:
    """Authentication context for identity enforcement."""
    semantic_context_key: str = "semantic_context"  #: :meta private:
    """Semantic context for semantic enforcement."""
    app_name: str  #: :meta private:
    """App name."""
    owner: str  #: :meta private:
    """Owner of app."""
    description: str  #: :meta private:
    """Description of app."""
    api_key: Optional[str] = None  #: :meta private:
    """Pebblo cloud API key for app."""
    classifier_url: str = CLASSIFIER_URL  #: :meta private:
    """Classifier endpoint."""
    _discover_sent: bool = False  #: :meta private:
    """Flag to check if discover payload has been sent."""
    _prompt_sent: bool = False  #: :meta private:
    """Flag to check if prompt payload has been sent."""

    def _call(
        self,
        inputs: Dict[str, Any],
        run_manager: Optional[CallbackManagerForChainRun] = None,
    ) -> Dict[str, Any]:
        """Run get_relevant_text and llm on input query.

        If chain has 'return_source_documents' as 'True', returns
        the retrieved documents as well under the key 'source_documents'.

        Example:
        .. code-block:: python

        res = indexqa({'query': 'This is my query'})
        answer, docs = res['result'], res['source_documents']
        """
        prompt_time = datetime.datetime.now().isoformat()
        _run_manager = run_manager or CallbackManagerForChainRun.get_noop_manager()
        question = inputs[self.input_key]
        auth_context = inputs.get(self.auth_context_key, {})
        semantic_context = inputs.get(self.semantic_context_key, {})
        accepts_run_manager = (
            "run_manager" in inspect.signature(self._get_docs).parameters
        )
        if accepts_run_manager:
            docs = self._get_docs(
                question, auth_context, semantic_context, run_manager=_run_manager
            )
        else:
            docs = self._get_docs(question, auth_context, semantic_context)  # type: ignore[call-arg]
        answer = self.combine_documents_chain.run(
            input_documents=docs, question=question, callbacks=_run_manager.get_child()
        )

        qa = {
            "name": self.app_name,
            "context": [
                {
                    "retrieved_from": doc.metadata.get("source"),
                    "doc": doc.page_content,
                    "vector_db": self.retriever.vectorstore.__class__.__name__,
                }
                for doc in docs
                if isinstance(doc, Document)
            ],
            "prompt": {"data": question},
            "response": {
                "data": answer,
            },
            "prompt_time": prompt_time,
            "user": auth_context.user_id if auth_context else "unknown",
            "user_identities": auth_context.user_auth
            if auth_context and hasattr(auth_context, "user_auth")
            else [],
        }
        qa_payload = Qa(**qa)
        self._send_prompt(qa_payload)

        if self.return_source_documents:
            return {self.output_key: answer, "source_documents": docs}
        else:
            return {self.output_key: answer}

    async def _acall(
        self,
        inputs: Dict[str, Any],
        run_manager: Optional[AsyncCallbackManagerForChainRun] = None,
    ) -> Dict[str, Any]:
        """Run get_relevant_text and llm on input query.

        If chain has 'return_source_documents' as 'True', returns
        the retrieved documents as well under the key 'source_documents'.

        Example:
        .. code-block:: python

        res = indexqa({'query': 'This is my query'})
        answer, docs = res['result'], res['source_documents']
        """
        _run_manager = run_manager or AsyncCallbackManagerForChainRun.get_noop_manager()
        question = inputs[self.input_key]
        auth_context = inputs.get(self.auth_context_key)
        semantic_context = inputs.get(self.semantic_context_key)
        accepts_run_manager = (
            "run_manager" in inspect.signature(self._aget_docs).parameters
        )
        if accepts_run_manager:
            docs = await self._aget_docs(
                question, auth_context, semantic_context, run_manager=_run_manager
            )
        else:
            docs = await self._aget_docs(question, auth_context, semantic_context)  # type: ignore[call-arg]
        answer = await self.combine_documents_chain.arun(
            input_documents=docs, question=question, callbacks=_run_manager.get_child()
        )

        if self.return_source_documents:
            return {self.output_key: answer, "source_documents": docs}
        else:
            return {self.output_key: answer}

    class Config:
        """Configuration for this pydantic object."""

        extra = Extra.forbid
        arbitrary_types_allowed = True
        allow_population_by_field_name = True

    @property
    def input_keys(self) -> List[str]:
        """Input keys.

        :meta private:
        """
        return [self.input_key, self.auth_context_key, self.semantic_context_key]

    @property
    def output_keys(self) -> List[str]:
        """Output keys.

        :meta private:
        """
        _output_keys = [self.output_key]
        if self.return_source_documents:
            _output_keys += ["source_documents"]
        return _output_keys

    @property
    def _chain_type(self) -> str:
        """Return the chain type."""
        return "pebblo_retrieval_qa"

    @classmethod
    def from_chain_type(
        cls,
        llm: BaseLanguageModel,
        app_name: str,
        description: str,
        owner: str,
        chain_type: str = "stuff",
        chain_type_kwargs: Optional[dict] = None,
        api_key: Optional[str] = None,
        classifier_url: str = CLASSIFIER_URL,
        **kwargs: Any,
    ) -> "PebbloRetrievalQA":
        """Load chain from chain type."""
        from langchain.chains.question_answering import load_qa_chain

        _chain_type_kwargs = chain_type_kwargs or {}
        combine_documents_chain = load_qa_chain(
            llm, chain_type=chain_type, **_chain_type_kwargs
        )

        # generate app
        app = PebbloRetrievalQA._get_app_details(
            app_name=app_name,
            description=description,
            owner=owner,
            llm=llm,
            **kwargs,
        )

        PebbloRetrievalQA._send_discover(
            app, api_key=api_key, classifier_url=classifier_url
        )

        return cls(
            combine_documents_chain=combine_documents_chain,
            app_name=app_name,
            owner=owner,
            description=description,
            api_key=api_key,
            classifier_url=classifier_url,
            **kwargs,
        )

    @validator("retriever", pre=True, always=True)
    def validate_vectorstore(
        cls, retriever: VectorStoreRetriever
    ) -> VectorStoreRetriever:
        """
        Validate that the vectorstore of the retriever is supported vectorstores.
        """
        if not any(
            isinstance(retriever.vectorstore, supported_class)
            for supported_class in SUPPORTED_VECTORSTORES
        ):
            raise ValueError(
                f"Vectorstore must be an instance of one of the supported "
                f"vectorstores: {SUPPORTED_VECTORSTORES}. "
                f"Got {type(retriever.vectorstore).__name__} instead."
            )
        return retriever

    def _get_docs(
        self,
        question: str,
        auth_context: Optional[AuthContext],
        semantic_context: Optional[SemanticContext],
        *,
        run_manager: CallbackManagerForChainRun,
    ) -> List[Document]:
        """Get docs."""
        set_enforcement_filters(self.retriever, auth_context, semantic_context)
        return self.retriever.get_relevant_documents(
            question, callbacks=run_manager.get_child()
        )

    async def _aget_docs(
        self,
        question: str,
        auth_context: Optional[AuthContext],
        semantic_context: Optional[SemanticContext],
        *,
        run_manager: AsyncCallbackManagerForChainRun,
    ) -> List[Document]:
        """Get docs."""
        set_enforcement_filters(self.retriever, auth_context, semantic_context)
        return await self.retriever.aget_relevant_documents(
            question, callbacks=run_manager.get_child()
        )

    @staticmethod
    def _get_app_details(app_name, owner, description, llm, **kwargs) -> App:  # type: ignore
        """Fetch app details. Internal method.
        Returns:
            App: App details.
        """
        framework, runtime = get_runtime()
        chains = PebbloRetrievalQA.get_chain_details(llm, **kwargs)
        app = App(
            name=app_name,
            owner=owner,
            description=description,
            runtime=runtime,
            framework=framework,
            chains=chains,
            plugin_version=PLUGIN_VERSION,
        )
        return app

    @staticmethod
    def _send_discover(app, api_key, classifier_url) -> None:  # type: ignore
        """Send app discovery payload to pebblo-server. Internal method."""
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        payload = app.dict(exclude_unset=True)
        app_discover_url = f"{classifier_url}{APP_DISCOVER_URL}"
        try:
            pebblo_resp = requests.post(
                app_discover_url, headers=headers, json=payload, timeout=20
            )
            logger.debug("discover-payload: %s", payload)
            logger.debug(
                "send_discover[local]: request url %s, body %s len %s\
                    response status %s body %s",
                pebblo_resp.request.url,
                str(pebblo_resp.request.body),
                str(len(pebblo_resp.request.body if pebblo_resp.request.body else [])),
                str(pebblo_resp.status_code),
                pebblo_resp.json(),
            )
            if pebblo_resp.status_code in [HTTPStatus.OK, HTTPStatus.BAD_GATEWAY]:
                PebbloRetrievalQA.set_discover_sent()
            else:
                logger.warning(
                    f"Received unexpected HTTP response code: {pebblo_resp.status_code}"
                )
        except requests.exceptions.RequestException:
            logger.warning("Unable to reach pebblo server.")
        except Exception as e:
            logger.warning("An Exception caught in _send_discover: local %s", e)

        if api_key:
            try:
                headers.update({"x-api-key": api_key})
                pebblo_cloud_url = f"{PEBBLO_CLOUD_URL}{APP_DISCOVER_URL}"
                pebblo_cloud_response = requests.post(
                    pebblo_cloud_url, headers=headers, json=payload, timeout=20
                )

                logger.debug(
                    "send_discover[cloud]: request url %s, body %s len %s\
                        response status %s body %s",
                    pebblo_cloud_response.request.url,
                    str(pebblo_cloud_response.request.body),
                    str(
                        len(
                            pebblo_cloud_response.request.body
                            if pebblo_cloud_response.request.body
                            else []
                        )
                    ),
                    str(pebblo_cloud_response.status_code),
                    pebblo_cloud_response.json(),
                )
            except requests.exceptions.RequestException:
                logger.warning("Unable to reach Pebblo cloud server.")
            except Exception as e:
                logger.warning("An Exception caught in _send_discover: cloud %s", e)

    @classmethod
    def set_discover_sent(cls) -> None:
        cls._discover_sent = True

    @classmethod
    def set_prompt_sent(cls) -> None:
        cls._prompt_sent = True

    def _send_prompt(self, qa_payload: Qa) -> None:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        app_discover_url = f"{self.classifier_url}{PROMPT_URL}"
        try:
            pebblo_resp = requests.post(
                app_discover_url, headers=headers, json=qa_payload.dict(), timeout=20
            )
            logger.debug("prompt-payload: %s", qa_payload)
            logger.debug(
                "send_prompt[local]: request url %s, body %s len %s\
                    response status %s body %s",
                pebblo_resp.request.url,
                str(pebblo_resp.request.body),
                str(len(pebblo_resp.request.body if pebblo_resp.request.body else [])),
                str(pebblo_resp.status_code),
                pebblo_resp.json(),
            )
            if pebblo_resp.status_code in [HTTPStatus.OK, HTTPStatus.BAD_GATEWAY]:
                PebbloRetrievalQA.set_prompt_sent()
            else:
                logger.warning(
                    f"Received unexpected HTTP response code: {pebblo_resp.status_code}"
                )
        except requests.exceptions.RequestException:
            logger.warning("Unable to reach pebblo server.")
        except Exception as e:
            logger.warning("An Exception caught in _send_discover: local %s", e)

        if self.api_key:
            try:
                headers.update({"x-api-key": self.api_key})
                pebblo_cloud_url = f"{PEBBLO_CLOUD_URL}{PROMPT_URL}"
                pebblo_cloud_response = requests.post(
                    pebblo_cloud_url,
                    headers=headers,
                    json=qa_payload.dict(),
                    timeout=20,
                )

                logger.debug(
                    "send_prompt[cloud]: request url %s, body %s len %s\
                        response status %s body %s",
                    pebblo_cloud_response.request.url,
                    str(pebblo_cloud_response.request.body),
                    str(
                        len(
                            pebblo_cloud_response.request.body
                            if pebblo_cloud_response.request.body
                            else []
                        )
                    ),
                    str(pebblo_cloud_response.status_code),
                    pebblo_cloud_response.json(),
                )
            except requests.exceptions.RequestException:
                logger.warning("Unable to reach Pebblo cloud server.")
            except Exception as e:
                logger.warning("An Exception caught in _send_prompt: cloud %s", e)

    @classmethod
    def get_chain_details(cls, llm, **kwargs):  # type: ignore
        llm_dict = llm.__dict__
        chain = [
            {
                "name": cls.__name__,
                "model": {
                    "name": llm_dict.get("model_name", llm_dict.get("model")),
                    "vendor": llm.__class__.__name__,
                },
                "vector_dbs": [
                    {
                        "name": kwargs["retriever"].vectorstore.__class__.__name__,
                        "embedding_model": str(
                            kwargs["retriever"].vectorstore._embeddings.model
                        )
                        if hasattr(kwargs["retriever"].vectorstore, "_embeddings")
                        else (
                            str(kwargs["retriever"].vectorstore._embedding.model)
                            if hasattr(kwargs["retriever"].vectorstore, "_embedding")
                            else None
                        ),
                    }
                ],
            }
        ]
        return chain
