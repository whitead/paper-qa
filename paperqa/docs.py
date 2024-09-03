from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO, Callable, Coroutine, cast
from uuid import UUID, uuid4

from openai import AsyncOpenAI
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)
from tenacity import retry, stop_after_attempt

try:
    import voyageai

    USE_VOYAGE = True
except ImportError:
    USE_VOYAGE = False

from .clients import ALL_CLIENTS, DocMetadataClient
from .config import AnswerSettings, PromptSettings
from .core import llm_parse_json, map_fxn_summary, retrieve_texts
from .llms import (
    HybridEmbeddingModel,
    LLMModel,
    NumpyVectorStore,
    OpenAIEmbeddingModel,
    OpenAILLMModel,
    VectorStore,
    VoyageAIEmbeddingModel,
    is_anyscale_model,
    llm_model_factory,
    vector_store_factory,
)
from .paths import PAPERQA_DIR
from .readers import read_doc
from .types import (
    Answer,
    CallbackFactory,
    Context,
    Doc,
    DocDetails,
    DocKey,
    LLMResult,
    Text,
    set_llm_answer_ids,
)
from .utils import (
    gather_with_concurrency,
    get_loop,
    maybe_is_html,
    maybe_is_pdf,
    maybe_is_text,
    md5sum,
    name_in_text,
)


# this is just to reduce None checks/type checks
async def empty_callback(result: LLMResult):
    pass


async def print_callback(result: LLMResult):
    pass


class Docs(BaseModel):
    """A collection of documents to be used for answering questions."""

    model_config = ConfigDict(extra="forbid")

    # ephemeral vars that should not be pickled (_things)
    _client: Any | None = None
    _embedding_client: Any | None = None
    id: UUID = Field(default_factory=uuid4)
    llm: str = "default"
    summary_llm: str | None = None
    llm_model: LLMModel = Field(
        default=OpenAILLMModel(
            config={"model": "gpt-4-0125-preview", "temperature": 0.1}
        )
    )
    summary_llm_model: LLMModel | None = Field(default=None, validate_default=True)
    embedding: str | None = "default"
    docs: dict[DocKey, Doc | DocDetails] = {}
    texts: list[Text] = []
    docnames: set[str] = set()
    texts_index: VectorStore[Text] = Field(default_factory=NumpyVectorStore)
    docs_index: VectorStore[Docs] = Field(default_factory=NumpyVectorStore)
    name: str = Field(default="default", description="Name of this docs collection")
    index_path: Path | None = Field(
        default=PAPERQA_DIR, description="Path to save index", validate_default=True
    )
    deleted_dockeys: set[DocKey] = set()
    jit_texts_index: bool = False

    def __init__(self, **data):
        # We do it here because we need to move things to private attributes
        embedding_client: Any | None = None
        client: Any | None = None
        if "embedding_client" in data:
            embedding_client = data.pop("embedding_client")
        if "client" in data:
            client = data.pop("client")
        # backwards compatibility
        if "doc_index" in data:
            data["docs_index"] = data.pop("doc_index")
        super().__init__(**data)
        self.set_client(client, embedding_client)

    @field_validator("index_path")
    @classmethod
    def handle_default(cls, value: Path | None, info: ValidationInfo) -> Path | None:
        if value == PAPERQA_DIR:
            return PAPERQA_DIR / info.data["name"]
        return value

    @model_validator(mode="before")
    @classmethod
    def setup_alias_models(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "llm" in data and data["llm"] != "default":
                data["llm_model"] = llm_model_factory(data["llm"])
            if "summary_llm" in data and data["summary_llm"] is not None:
                data["summary_llm_model"] = llm_model_factory(data["summary_llm"])
            if "embedding" in data and data["embedding"] != "default":
                if "texts_index" not in data:
                    data["texts_index"] = vector_store_factory(data["embedding"])
                if "docs_index" not in data:
                    data["docs_index"] = vector_store_factory(data["embedding"])
        return data

    @model_validator(mode="after")
    @classmethod
    def config_summary_llm_config(cls, data: Any) -> Any:
        if isinstance(data, Docs):
            # check our default gpt-4/3.5-turbo config
            # default check is hard - becauise either llm is set or llm_model is set
            if (
                data.summary_llm_model is None
                and data.llm == "default"
                and isinstance(data.llm_model, OpenAILLMModel)
            ):
                data.summary_llm_model = OpenAILLMModel(
                    config={"model": "gpt-4o-mini", "temperature": 0.1}
                )
            elif data.summary_llm_model is None:
                data.summary_llm_model = data.llm_model
        return data

    @classmethod
    def make_llm_names_consistent(cls, data: Any) -> Any:
        if isinstance(data, Docs):
            data.llm = data.llm_model.name
            if data.llm == "langchain":
                # from langchain models - kind of hacky
                # langchain models cannot know type until
                # it sees client
                data.llm_model.infer_llm_type(data._client)
                data.llm = data.llm_model.name
            if data.summary_llm_model is not None:
                if (
                    data.summary_llm is None
                    and data.summary_llm_model is data.llm_model
                ):
                    data.summary_llm = data.llm
                if data.summary_llm == "langchain":
                    # from langchain models - kind of hacky
                    data.summary_llm_model.infer_llm_type(data._client)
                    data.summary_llm = data.summary_llm_model.name
            data.embedding = data.texts_index.embedding_model.name
        return data

    def clear_docs(self):
        self.texts = []
        self.docs = {}
        self.docnames = set()

    def __getstate__(self):
        # You may wonder why make these private if we're just going
        # to be overriding the behavior on setstaet/getstate anyway.
        # The reason is that the other serialization methods from Pydantic -
        # model_dump - will not drop private attributes.
        # So - this getstate/setstate removes private attributes for pickling
        # and Pydantic will handle removing private attributes for other
        # serialization methods (like model_dump_json)
        state = super().__getstate__()
        # remove client from private attributes
        del state["__pydantic_private__"]["_client"]
        del state["__pydantic_private__"]["_embedding_client"]
        return state

    def __setstate__(self, state):
        # add client back to private attributes
        state["__pydantic_private__"]["_client"] = None
        state["__pydantic_private__"]["_embedding_client"] = None
        super().__setstate__(state)

    def set_client(
        self,
        client: Any | None = None,
        embedding_client: Any | None = None,
    ):
        if client is None and isinstance(self.llm_model, OpenAILLMModel):
            if is_anyscale_model(self.llm_model.name):
                client = AsyncOpenAI(
                    api_key=os.environ["ANYSCALE_API_KEY"],
                    base_url=os.environ["ANYSCALE_BASE_URL"],
                )
            else:
                client = AsyncOpenAI()
        self._client = client
        if embedding_client is None:
            # check if we have an openai embedding model in use
            if isinstance(self.texts_index.embedding_model, OpenAIEmbeddingModel) or (
                isinstance(self.texts_index.embedding_model, HybridEmbeddingModel)
                and any(
                    isinstance(m, OpenAIEmbeddingModel)
                    for m in self.texts_index.embedding_model.models
                )
            ):
                embedding_client = (
                    client if isinstance(client, AsyncOpenAI) else AsyncOpenAI()
                )
            elif USE_VOYAGE and (
                isinstance(self.texts_index.embedding_model, VoyageAIEmbeddingModel)
                or (
                    isinstance(self.texts_index.embedding_model, HybridEmbeddingModel)
                    and any(
                        isinstance(m, VoyageAIEmbeddingModel)
                        for m in self.texts_index.embedding_model.models
                    )
                )
            ):
                embedding_client = (
                    client
                    if isinstance(client, voyageai.AsyncClient)
                    else voyageai.AsyncClient()
                )
        self._embedding_client = embedding_client
        Docs.make_llm_names_consistent(self)

    def _get_unique_name(self, docname: str) -> str:
        """Create a unique name given proposed name."""
        suffix = ""
        while docname + suffix in self.docnames:
            # move suffix to next letter
            suffix = "a" if suffix == "" else chr(ord(suffix) + 1)
        docname += suffix
        return docname

    def add_file(
        self,
        file: BinaryIO,
        citation: str | None = None,
        docname: str | None = None,
        dockey: DocKey | None = None,
        chunk_chars: int = 3000,
    ) -> str | None:
        loop = get_loop()
        return loop.run_until_complete(
            self.aadd_file(
                file,
                citation=citation,
                docname=docname,
                dockey=dockey,
                chunk_chars=chunk_chars,
            )
        )

    async def aadd_file(
        self,
        file: BinaryIO,
        citation: str | None = None,
        docname: str | None = None,
        dockey: DocKey | None = None,
        chunk_chars: int = 3000,
        title: str | None = None,
        doi: str | None = None,
        authors: list[str] | None = None,
        use_doc_details: bool = False,
        **kwargs,
    ) -> str | None:
        """Add a document to the collection."""
        # just put in temp file and use existing method
        suffix = ".txt"
        if maybe_is_pdf(file):
            suffix = ".pdf"
        elif maybe_is_html(file):
            suffix = ".html"

        with tempfile.NamedTemporaryFile(suffix=suffix) as f:
            f.write(file.read())
            f.seek(0)
            return await self.aadd(
                Path(f.name),
                citation=citation,
                docname=docname,
                dockey=dockey,
                chunk_chars=chunk_chars,
                title=title,
                doi=doi,
                authors=authors,
                use_doc_details=use_doc_details,
                **kwargs,
            )

    def add_url(
        self,
        url: str,
        citation: str | None = None,
        docname: str | None = None,
        dockey: DocKey | None = None,
        chunk_chars: int = 3000,
    ) -> str | None:
        loop = get_loop()
        return loop.run_until_complete(
            self.aadd_url(
                url,
                citation=citation,
                docname=docname,
                dockey=dockey,
                chunk_chars=chunk_chars,
            )
        )

    async def aadd_url(
        self,
        url: str,
        citation: str | None = None,
        docname: str | None = None,
        dockey: DocKey | None = None,
        chunk_chars: int = 3000,
    ) -> str | None:
        """Add a document to the collection."""
        import urllib.request

        with urllib.request.urlopen(url) as f:  # noqa: ASYNC210, S310
            # need to wrap to enable seek
            file = BytesIO(f.read())
            return await self.aadd_file(
                file,
                citation=citation,
                docname=docname,
                dockey=dockey,
                chunk_chars=chunk_chars,
            )

    def add(
        self,
        path: Path,
        citation: str | None = None,
        docname: str | None = None,
        disable_check: bool = False,
        dockey: DocKey | None = None,
        chunk_chars: int = 3000,
        title: str | None = None,
        doi: str | None = None,
        authors: list[str] | None = None,
        use_doc_details: bool = False,
        **kwargs,
    ) -> str | None:
        loop = get_loop()
        return loop.run_until_complete(
            self.aadd(
                path,
                citation=citation,
                docname=docname,
                disable_check=disable_check,
                dockey=dockey,
                chunk_chars=chunk_chars,
                title=title,
                doi=doi,
                authors=authors,
                use_doc_details=use_doc_details,
                **kwargs,
            )
        )

    async def aadd(  # noqa: C901, PLR0912, PLR0915
        self,
        path: Path,
        citation: str | None = None,
        docname: str | None = None,
        disable_check: bool = False,
        dockey: DocKey | None = None,
        chunk_chars: int = 3000,
        overlap: int = 250,
        title: str | None = None,
        doi: str | None = None,
        authors: list[str] | None = None,
        use_doc_details: bool = False,
        **kwargs,
    ) -> str | None:
        """Add a document to the collection."""
        if dockey is None:
            dockey = md5sum(path)
        if citation is None:
            # skip system because it's too hesitant to answer
            cite_chain = self.llm_model.make_chain(
                client=self._client,
                prompt=self.prompts.cite,
                skip_system=True,
            )
            # peak first chunk
            fake_doc = Doc(docname="", citation="", dockey=dockey)
            texts = read_doc(path, fake_doc, chunk_chars=chunk_chars, overlap=overlap)
            if len(texts) == 0:
                raise ValueError(f"Could not read document {path}. Is it empty?")
            chain_result = await cite_chain({"text": texts[0].text}, None)
            citation = chain_result.text
            if (
                len(citation) < 3  # noqa: PLR2004
                or "Unknown" in citation
                or "insufficient" in citation
            ):
                citation = f"Unknown, {os.path.basename(path)}, {datetime.now().year}"

        if docname is None:
            # get first name and year from citation
            match = re.search(r"([A-Z][a-z]+)", citation)
            if match is not None:
                author = match.group(1)
            else:
                # panicking - no word??
                raise ValueError(
                    f"Could not parse docname from citation {citation}. "
                    "Consider just passing key explicitly - e.g. docs.py "
                    "(path, citation, key='mykey')"
                )
            year = ""
            match = re.search(r"(\d{4})", citation)
            if match is not None:
                year = match.group(1)
            docname = f"{author}{year}"
        docname = self._get_unique_name(docname)

        doc = Doc(docname=docname, citation=citation, dockey=dockey)

        # try to extract DOI / title from the citation
        if (doi is title is None) and use_doc_details:
            structured_cite_chain = self.llm_model.make_chain(
                client=self._client,
                prompt=self.prompts.structured_cite,
                skip_system=True,
            )
            chain_result = await structured_cite_chain({"citation": citation}, None)
            with contextlib.suppress(json.JSONDecodeError):
                clean_text = chain_result.text.strip("`")
                if clean_text.startswith("json"):
                    clean_text = clean_text.replace("json", "", 1)
                citation_json = json.loads(clean_text)
                if citation_title := citation_json.get("title"):
                    title = citation_title
                if citation_doi := citation_json.get("doi"):
                    doi = citation_doi
                if citation_author := citation_json.get("authors"):
                    authors = citation_author

        # see if we can upgrade to DocDetails
        # if not, we can progress with a normal Doc
        # if "overwrite_fields_from_metadata" is used:
        # will map "docname" to "key", and "dockey" to "doc_id"
        if (title or doi) and use_doc_details:
            if kwargs.get("metadata_client"):
                metadata_client = kwargs["metadata_client"]
            else:
                metadata_client = DocMetadataClient(
                    session=kwargs.pop("session", None),
                    clients=kwargs.pop("clients", DEFAULT_CLIENTS),
                )

            query_kwargs: dict[str, Any] = {}

            if doi:
                query_kwargs["doi"] = doi
            if authors:
                query_kwargs["authors"] = authors
            if title:
                query_kwargs["title"] = title

            doc = await metadata_client.upgrade_doc_to_doc_details(
                doc, **(query_kwargs | kwargs)
            )

        texts = read_doc(path, doc, chunk_chars=chunk_chars, overlap=100)
        # loose check to see if document was loaded
        if (
            len(texts) == 0
            or len(texts[0].text) < 10  # noqa: PLR2004
            or (not disable_check and not maybe_is_text(texts[0].text))
        ):
            raise ValueError(
                f"This does not look like a text document: {path}. Pass disable_check to ignore this error."
            )
        if await self.aadd_texts(texts, doc):
            return docname
        return None

    def add_texts(
        self,
        texts: list[Text],
        doc: Doc,
    ) -> bool:
        loop = get_loop()
        return loop.run_until_complete(self.aadd_texts(texts, doc))

    async def aadd_texts(self, texts: list[Text], doc: Doc) -> bool:
        """
        Add chunked texts to the collection.

        NOTE: this is useful if you have already chunked the texts yourself.

        Returns:
            True if the doc was added, otherwise False if already in the collection.
        """
        if doc.dockey in self.docs:
            return False
        if not texts:
            raise ValueError("No texts to add.")
        # 1. Calculate text embeddings if not already present, but don't set them into
        # the texts until we've set up the Doc's embedding, so callers can retry upon
        # OpenAI rate limit errors
        text_embeddings: list[list[float]] | None = (
            await self.texts_index.embedding_model.embed_documents(
                self._embedding_client, texts=[t.text for t in texts]
            )
            if texts[0].embedding is None
            else None
        )
        # 2. Set the Doc's embedding to be the Doc's citation embedded
        if doc.embedding is None:
            doc.embedding = (
                await self.docs_index.embedding_model.embed_documents(
                    self._embedding_client, texts=[doc.citation]
                )
            )[0]
        # 3. Now we can set the text embeddings
        if text_embeddings is not None:
            for t, t_embedding in zip(texts, text_embeddings, strict=True):
                t.embedding = t_embedding
        # 4. Update texts and the Doc's name
        if doc.docname in self.docnames:
            new_docname = self._get_unique_name(doc.docname)
            for t in texts:
                t.name = t.name.replace(doc.docname, new_docname)
            doc.docname = new_docname
        # 5. Index remaining updates
        if not self.jit_texts_index:
            self.texts_index.add_texts_and_embeddings(texts)
        self.docs_index.add_texts_and_embeddings([doc])
        self.docs[doc.dockey] = doc
        self.texts += texts
        self.docnames.add(doc.docname)
        return True

    def delete(
        self,
        name: str | None = None,
        docname: str | None = None,
        dockey: DocKey | None = None,
    ) -> None:
        """Delete a document from the collection."""
        # name is an alias for docname
        name = docname if name is None else name

        if name is not None:
            doc = next((doc for doc in self.docs.values() if doc.docname == name), None)
            if doc is None:
                return
            self.docnames.remove(doc.docname)
            dockey = doc.dockey
        del self.docs[dockey]
        self.deleted_dockeys.add(dockey)
        self.texts = list(filter(lambda x: x.doc.dockey != dockey, self.texts))

    def _build_texts_index(self, keys: set[DocKey] | None = None):
        texts = self.texts
        if keys is not None and self.jit_texts_index:
            # TODO: what is JIT even for??
            if keys is not None:
                texts = [t for t in texts if t.doc.dockey in keys]
            if len(texts) == 0:
                return
            self.texts_index.clear()
            self.texts_index.add_texts_and_embeddings(texts)
        if self.jit_texts_index and keys is None:
            # Not sure what else to do here???????
            print(
                "Warning: JIT text index without keys "
                "requires rebuilding index each time!"
            )
            self.texts_index.clear()
            self.texts_index.add_texts_and_embeddings(texts)

    async def retrieve_texts(self, query: str, k: int) -> list[Text]:
        _k = k + len(self.deleted_dockeys)
        matches = await self.index.max_marginal_relevance_search(
            self._embedding_client, query, k=_k, fetch_k=2 * _k
        )[0]
        matches = [m for m in matches if m.text.doc.dockey not in self.deleted_dockeys]
        return matches[:k]

    def get_evidence(
        self,
        query: str,
        exclude_text_filter: set[str] | None = None,
        answer_config: AnswerSettings | None = None,
        prompt_config: PromptSettings | None = None,
        callbacks: list[Callable] | None = None,
    ) -> list[Context]:
        return get_loop().run_until_complete(
            self.aget_evidence(
                query,
                exclude_text_filter=exclude_text_filter,
                answer_config=answer_config,
                prompt_config=prompt_config,
                callbacks=callbacks,
            )
        )

    async def aget_evidence(
        self,
        answer: Answer,
        exclude_text_filter: set[str] | None = None,
        answer_config: AnswerSettings | None = None,
        prompt_config: PromptSettings | None = None,
        callbacks: list[Callable] | None = None,
    ) -> list[Context]:

        # Stuff we're missing:
        # JIT
        if len(self.docs) == 0:
            return []

        if answer_config is None:
            answer_config = AnswerSettings()

        if exclude_text_filter:
            _k = answer_config.doc_match_k + len(
                exclude_text_filter
            )  # heuristic - get enough so we can downselect

        matches = await self.retrieve_texts(answer.query, _k)

        if exclude_text_filter:
            matches = [m for m in matches if m.text not in exclude_text_filter]

        summary_chain = None

        if not prompt_config.skip_summary:
            if prompt_config.json_summary:
                summary_chain = self.summary_llm_model.make_chain(  # type: ignore[union-attr]
                    client=self._client,
                    prompt=prompt_config.summary_json,
                    system_prompt=prompt_config.summary_json_system,
                )
            else:
                summary_chain = self.summary_llm_model.make_chain(  # type: ignore[union-attr]
                    client=self._client,
                    prompt=prompt_config.summary,
                    system_prompt=prompt_config.system,
                )

        with set_llm_answer_ids(answer.id):
            results = await gather_with_concurrency(
                self.max_concurrent,
                [
                    map_fxn_summary(
                        m,
                        answer.query,
                        summary_chain,
                        {"summary_length": answer_config.evidence_summary_length},
                        llm_parse_json if prompt_config.json_summary else None,
                        callbacks,
                    )
                    for m in matches
                ],
            )

        for _, llm_result in results:
            answer.add_tokens(llm_result)

        # add non-zero
        return [r for r, _ in results if r is not None and r.score > 0]

    def query(
        self,
        answer: Answer | str,
        answer_config: AnswerSettings | None = None,
        prompt_config: PromptSettings | None = None,
        callbacks: list[Callable] | None = None,
    ) -> Answer:
        return get_loop().run_until_complete(
            self.aquery(
                answer,
                answer_config=answer_config,
                prompt_config=prompt_config,
                callbacks=callbacks,
            )
        )

    async def aquery(  # noqa: C901, PLR0912, PLR0915
        self,
        answer: Answer | str,
        answer_config: AnswerSettings | None = None,
        prompt_config: PromptSettings | None = None,
        callbacks: list[Callable] | None = None,
    ) -> Answer:

        if isinstance(answer, str):
            answer = Answer(question=answer)

        if answer_config is None:
            answer_config = AnswerSettings()

        if prompt_config is None:
            prompt_config = PromptSettings()

        contexts = answer.contexts

        if not contexts:
            contexts = await self.aget_evidence(
                answer.question,
                answer_config=answer_config,
                prompt_config=prompt_config,
                callbacks=callbacks,
            )
        pre_str = None
        if prompt_config.pre is not None:
            chain = self.llm_model.make_chain(
                client=self._client,
                prompt=prompt_config.pre,
                system_prompt=prompt_config.system,
            )
            with set_llm_answer_ids(answer.id):
                pre = await chain({"question": answer.question}, callbacks, name="pre")
            answer.add_tokens(pre)
            pre_str = pre.text

        filtered_contexts = sorted(
            contexts,
            key=lambda x: x.score,
            reverse=True,
        )[: answer_config.answer_max_sources]

        context_str = "\n\n".join(
            [
                f"{c.text.name}: {c.context}"
                + "".join([f"\n{k}: {v}" for k, v in (c.model_extra or {}).items()])
                + (
                    f"\nFrom {c.text.doc.citation}"
                    if answer_config.evidence_detailed_citations
                    else ""
                )
                for c in filtered_contexts
            ]
            + [f"Extra background information: {pre_str}"]
            if pre_str
            else []
        )

        valid_names = [c.text.name for c in filtered_contexts]
        context_str += "\n\nValid keys: " + ", ".join(valid_names)

        bib = {}
        if len(context_str) < 10:  # noqa: PLR2004
            answer_text = (
                "I cannot answer this question due to insufficient information."
            )
        else:
            qa_chain = self.llm_model.make_chain(
                client=self._client,
                prompt=prompt_config.qa,
                system_prompt=prompt_config.system,
            )
            with set_llm_answer_ids(answer.id):
                answer_result = await qa_chain(
                    {
                        "context": context_str,
                        "answer_length": answer.answer_length,
                        "question": answer.question,
                        "example_citation": PromptSettings.EXAMPLE_CITATION,
                    },
                    name="answer",
                )
            answer_text = answer_result.text
            answer.add_tokens(answer_result)
        # it still happens
        if PromptSettings.EXAMPLE_CITATION in answer_text:
            answer_text = answer_text.replace(PromptSettings.EXAMPLE_CITATION, "")
        for c in filtered_contexts:
            name = c.text.name
            citation = c.text.doc.citation
            # do check for whole key (so we don't catch Callahan2019a with Callahan2019)
            if name_in_text(name, answer_text):
                bib[name] = citation
        bib_str = "\n\n".join(
            [f"{i+1}. ({k}): {c}" for i, (k, c) in enumerate(bib.items())]
        )
        formatted_answer = f"Question: {answer.question}\n\n{answer_text}\n"
        if len(bib) > 0:
            formatted_answer += f"\nReferences\n\n{bib_str}\n"

        if prompt_config.post is not None:
            chain = self.llm_model.make_chain(
                client=self._client,
                prompt=prompt_config.post,
                system_prompt=prompt_config.system,
            )
            with set_llm_answer_ids(answer.id):
                post = await chain(answer.model_dump(), name="post")
            answer_text = post.text
            answer.add_tokens(post)
            formatted_answer = f"Question: {answer.question}\n\n{post}\n"
            if len(bib) > 0:
                formatted_answer += f"\nReferences\n\n{bib_str}\n"

        # now at end we modify, so we could have retried earlier
        answer.answer = answer_text
        answer.formatted_answer = formatted_answer
        answer.references = bib_str
        answer.contexts = contexts

        return answer
