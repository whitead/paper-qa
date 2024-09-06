from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import cast

from rich.table import Table

from .. import (
    Docs,
    embedding_model_factory,
)
from ..llms import LiteLLMModel
from .models import AnswerResponse, QueryRequest

logger = logging.getLogger(__name__)


def get_year(ts: datetime | None = None) -> str:
    """Get the year from the input datetime, otherwise using the current datetime."""
    if ts is None:
        ts = datetime.now()
    return ts.strftime("%Y")


async def litellm_get_search_query(
    question: str,
    count: int,
    template: str | None = None,
    llm: str = "gpt-4o-mini",
    temperature: float = 1.0,
) -> list[str]:
    if isinstance(template, str):
        if not (
            "{count}" in template and "{question}" in template and "{date}" in template
        ):
            logger.warning(
                "Template does not contain {count}, {question} and {date} variables. Ignoring template."
            )
            template = None

        else:
            # partial formatting
            search_prompt = template.replace("{date}", get_year())

    if template is None:
        search_prompt = (
            "We want to answer the following question: {question} \n"
            "Provide {count} unique keyword searches (one search per line) and year ranges "
            "that will find papers to help answer the question. "
            "Do not use boolean operators. "
            "Make sure not to repeat searches without changing the keywords or year ranges. "
            "Make some searches broad and some narrow. "
            "Use this format: [keyword search], [start year]-[end year]. "
            "where end year is optional. "
            f"The current year is {get_year()}."
        )

    if "gpt" not in llm:
        raise ValueError(
            f"Invalid llm: {llm}, note a GPT model must be used for the fake agent search."
        )
    model = LiteLLMModel(name=llm)
    model.config["model_list"][0]["litellm_params"].update({"temperature": temperature})
    chain = model.make_chain(client=None, prompt=search_prompt, skip_system=True)
    result = await chain({"question": question, "count": count})  # type: ignore[call-arg]
    search_query = result.text
    queries = [s for s in search_query.split("\n") if len(s) > 3]  # noqa: PLR2004
    # remove "2.", "3.", etc. -- https://regex101.com/r/W2f7F1/1
    queries = [re.sub(r"^\d+\.\s*", "", q) for q in queries]
    # remove quotes
    return [re.sub(r'["\[\]]', "", q) for q in queries]


def table_formatter(
    objects: list[tuple[AnswerResponse | Docs, str]], max_chars_per_column: int = 2000
) -> Table:
    example_object, _ = objects[0]
    if isinstance(example_object, AnswerResponse):
        table = Table(title="Prior Answers")
        table.add_column("Question", style="cyan")
        table.add_column("Answer", style="magenta")
        for obj, _ in objects:
            table.add_row(
                cast(AnswerResponse, obj).answer.question[:max_chars_per_column],
                cast(AnswerResponse, obj).answer.answer[:max_chars_per_column],
            )
        return table
    if isinstance(example_object, Docs):
        table = Table(title="PDF Search")
        table.add_column("Title", style="cyan")
        table.add_column("File", style="magenta")
        for obj, filename in objects:
            table.add_row(
                cast(Docs, obj).texts[0].doc.title[:max_chars_per_column], filename  # type: ignore[attr-defined]
            )
        return table
    raise NotImplementedError(
        f"Object type {type(example_object)} can not be converted to table."
    )


# Index 0 is for prompt tokens, index 1 is for completion tokens
costs: dict[str, tuple[float, float]] = {
    "claude-2": (11.02 / 10**6, 32.68 / 10**6),
    "claude-instant-1": (1.63 / 10**6, 5.51 / 10**6),
    "claude-3-sonnet-20240229": (3 / 10**6, 15 / 10**6),
    "claude-3-5-sonnet-20240620": (3 / 10**6, 15 / 10**6),
    "claude-3-opus-20240229": (15 / 10**6, 75 / 10**6),
    "babbage-002": (0.0004 / 10**3, 0.0004 / 10**3),
    "gpt-3.5-turbo": (0.0010 / 10**3, 0.0020 / 10**3),
    "gpt-3.5-turbo-1106": (0.0010 / 10**3, 0.0020 / 10**3),
    "gpt-3.5-turbo-0613": (0.0010 / 10**3, 0.0020 / 10**3),
    "gpt-3.5-turbo-0301": (0.0010 / 10**3, 0.0020 / 10**3),
    "gpt-3.5-turbo-0125": (0.0005 / 10**3, 0.0015 / 10**3),
    "gpt-4-1106-preview": (0.010 / 10**3, 0.030 / 10**3),
    "gpt-4-0125-preview": (0.010 / 10**3, 0.030 / 10**3),
    "gpt-4-turbo-2024-04-09": (10 / 10**6, 30 / 10**6),
    "gpt-4-turbo": (10 / 10**6, 30 / 10**6),
    "gpt-4": (0.03 / 10**3, 0.06 / 10**3),
    "gpt-4-0613": (0.03 / 10**3, 0.06 / 10**3),
    "gpt-4-0314": (0.03 / 10**3, 0.06 / 10**3),
    "gpt-4o": (2.5 / 10**6, 10 / 10**6),
    "gpt-4o-2024-05-13": (5 / 10**6, 15 / 10**6),
    "gpt-4o-2024-08-06": (2.5 / 10**6, 10 / 10**6),
    "gpt-4o-mini": (0.15 / 10**6, 0.60 / 10**6),
    "gemini-1.5-flash": (0.35 / 10**6, 0.35 / 10**6),
    "gemini-1.5-pro": (3.5 / 10**6, 10.5 / 10**6),
    # supported Anyscale models per
    # https://docs.anyscale.com/endpoints/text-generation/query-a-model
    "meta-llama/Meta-Llama-3-8B-Instruct": (0.15 / 10**6, 0.15 / 10**6),
    "meta-llama/Meta-Llama-3-70B-Instruct": (1.0 / 10**6, 1.0 / 10**6),
    "mistralai/Mistral-7B-Instruct-v0.1": (0.15 / 10**6, 0.15 / 10**6),
    "mistralai/Mixtral-8x7B-Instruct-v0.1": (1.0 / 10**6, 1.0 / 10**6),
    "mistralai/Mixtral-8x22B-Instruct-v0.1": (1.0 / 10**6, 1.0 / 10**6),
}


def compute_model_token_cost(model: str, tokens: int, is_completion: bool) -> float:
    if model in costs:  # Prefer our internal costs model
        model_costs: tuple[float, float] = costs[model]
    else:
        logger.warning(f"Model {model} not found in costs.")
        return 0.0
    return tokens * model_costs[int(is_completion)]


def compute_total_model_token_cost(token_counts: dict[str, list[int]]) -> float:
    """Sum the token counts for each model and return the total cost."""
    cost = 0.0
    for model, tokens in token_counts.items():
        if sum(tokens) > 0:
            cost += compute_model_token_cost(
                model, tokens=tokens[0], is_completion=False
            ) + compute_model_token_cost(model, tokens=tokens[1], is_completion=True)
    return cost


# the defaults here should be (about) the same as in QueryRequest
def update_doc_models(doc: Docs, request: QueryRequest | None = None):
    if request is None:
        request = QueryRequest()

    doc.llm_model = LiteLLMModel(name=request.settings.llm)
    doc.summary_llm_model = LiteLLMModel(name=request.settings.summary_llm)

    # set temperatures
    doc.llm_model.config["model_list"][0]["litellm_params"].update(
        {"temperature": request.settings.temperature}
    )
    doc.summary_llm_model.config["model_list"][0]["litellm_params"].update(
        {"temperature": request.settings.temperature}
    )

    doc.texts_index.embedding_model = embedding_model_factory(
        request.settings.embedding, **(request.settings.embedding_config or {})
    )
    doc.texts_index.mmr_lambda = request.settings.texts_index_mmr_lambda
    doc.embedding = request.settings.embedding
    Docs.make_llm_names_consistent(doc)

    logger.debug(
        f"update_doc_models: {doc.name}"
        f" | {(doc.llm_model.config)} | {(doc.summary_llm_model.config)}"
    )
