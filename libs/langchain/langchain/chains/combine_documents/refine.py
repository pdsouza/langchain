"""Combine documents by doing a first pass and then refining on more documents."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from langchain_core.documents import Document
from langchain_core.language_models import LanguageModelLike
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import BasePromptTemplate, format_document
from langchain_core.pydantic_v1 import Extra, Field, root_validator
from langchain_core.runnables import (
    Runnable,
    RunnableLambda,
    RunnablePassthrough,
)

from langchain.callbacks.manager import Callbacks
from langchain.chains.combine_documents.base import (
    DEFAULT_DOCUMENT_PROMPT,
    DOCUMENTS_KEY,
    INTERMEDIATE_STEPS_KEY,
    BaseCombineDocumentsChain,
    validate_prompt,
)
from langchain.chains.llm import LLMChain

# --- LCEL Runnable chain --- #

OUTPUT_KEY = "output"


def create_refine_documents_chain(
    llm: LanguageModelLike,
    initial_prompt: BasePromptTemplate,
    refine_prompt: BasePromptTemplate,
    *,
    document_prompt: Optional[BasePromptTemplate] = None,
) -> Runnable:
    """Create a chain that feeds documents to a model one at a time and updates the output.
    
    Args:
        llm: Language model to use for responding.
        initial_prompt: The prompt to use on the first document. Must accept "context" 
            as one of the input variables. The first document will be passed in as 
            "context".
        refine_prompt: The prompt to use on all subsequent documents. Must accept 
            "context" and "output" as input variables. A document will be passed in as
            "context" and the refined output up to this iteration will be passed in as
            "output.
        document_prompt: Prompt used for formatting each document into a string. Input
            variables can be "page_content" or any metadata keys that are in all
            documents. "page_content" will automatically retrieve the
            `Document.page_content`, and all other inputs variables will be
            automatically retrieved from the `Document.metadata` dictionary. Default to
            a prompt that only contains `Document.page_content`.
        
    Returns:
        An LCEL `Runnable` chain. 
        
            Expects a dictionary as input with a list of `Document`s being passed under 
            the "context" key. 
            
            Returns a dictionary as output. The output dictionary contains two keys, 
            "output" and "intermediate_steps". "output" contains the final output. 
            "intermediate_steps" contains the list of intermediate output 
            strings generated by the chain, in the order that they were generated.
        
    
    Example:
        .. code-block:: python
        
            # pip install -U langchain langchain-community
        
            from langchain_community.chat_models import ChatOpenAI
            from langchain_core.prompts import ChatPromptTemplate
            from langchain.chains.combine_documents.refine import create_refine_documents_chain

            initial_prompt = ChatPromptTemplate.from_messages([
                ("system", "Summarize this information: {context}"),
            ])
            refine_prompt = ChatPromptTemplate.from_messages([
                ("system", '''You are summarizing a long document one page at a time. \
            You have summarized part of the document. Given the next page, update your \
            summary. Respond with only the updated summary and no other text. \
            Here is your working summary:\n\n{output}.'''),
                ("human", "Here is the next page:\n\n{context}")
            ])
            llm = ChatOpenAI(model="gpt-3.5-turbo")
            chain = create_refine_documents_chain(llm, initial_prompt, refine_prompt, llm,)
            # chain.invoke({"context": docs})
    """  # noqa: E501
    validate_prompt(initial_prompt, (DOCUMENTS_KEY,))
    validate_prompt(refine_prompt, (DOCUMENTS_KEY, OUTPUT_KEY))

    format_doc: Runnable = RunnableLambda(_get_and_format_doc).bind(
        document_prompt=document_prompt or DEFAULT_DOCUMENT_PROMPT
    )

    # Runnable: Dict with many docs -> answer string based on first doc
    initial_response = format_doc.pipe(
        initial_prompt, llm, StrOutputParser(), name="initial_response"
    )

    # Runnable: Dict with many docs, current answer, intermediate_steps
    # -> updated answer based on next doc
    refine_response = format_doc.pipe(
        refine_prompt, llm, StrOutputParser(), name="refine_response"
    )

    # Runnable: Update intermediates_steps based on last output, in parallel update
    # output.
    refine_step = RunnablePassthrough.assign(
        intermediate_steps=_update_intermediate_steps,
        output=refine_response,
    )

    # Function that returns a sequence of refine_steps equal to len(docs) - 1.
    refine_loop = RunnableLambda(_runnable_loop).bind(
        step=refine_step, step_name="refine_step_{iteration}"
    )

    # Runnable: Dict with many docs -> {"answer": "...", "intermediate_steps": [...]}
    return (
        RunnablePassthrough.assign(output=initial_response)
        .pipe(refine_loop)
        .pick([OUTPUT_KEY, INTERMEDIATE_STEPS_KEY])
        .with_name("refine_documents_chain")
    )


# --- Helpers for LCEL Runnable chain --- #


def _get_and_format_doc(inputs: dict, document_prompt: BasePromptTemplate) -> dict:
    intermediate_steps = inputs.pop(INTERMEDIATE_STEPS_KEY, [])
    doc = inputs[DOCUMENTS_KEY][len(intermediate_steps)]
    inputs[DOCUMENTS_KEY] = format_document(doc, document_prompt)
    return inputs


def _runnable_loop(inputs: dict, step: Runnable, step_name: str) -> Runnable:
    if len(inputs[DOCUMENTS_KEY]) < 2:
        return RunnablePassthrough()
    chain: Runnable = step.with_name(step_name.format(iteration=1))
    for iteration in range(2, len(inputs[DOCUMENTS_KEY])):
        chain |= step.with_name(step_name.format(iteration=iteration))
    return chain


def _update_intermediate_steps(inputs: dict) -> list:
    return inputs.get(INTERMEDIATE_STEPS_KEY, []) + [inputs[OUTPUT_KEY]]


# --- Legacy Chain --- #


class RefineDocumentsChain(BaseCombineDocumentsChain):
    """Combine documents by doing a first pass and then refining on more documents.

    This algorithm first calls `initial_llm_chain` on the first document, passing
    that first document in with the variable name `document_variable_name`, and
    produces a new variable with the variable name `initial_response_name`.

    Then, it loops over every remaining document. This is called the "refine" step.
    It calls `refine_llm_chain`,
    passing in that document with the variable name `document_variable_name`
    as well as the previous response with the variable name `initial_response_name`.

    Example:
        .. code-block:: python

            from langchain.chains import RefineDocumentsChain, LLMChain
            from langchain_core.prompts import PromptTemplate
            from langchain.llms import OpenAI

            # This controls how each document will be formatted. Specifically,
            # it will be passed to `format_document` - see that function for more
            # details.
            document_prompt = PromptTemplate(
                input_variables=["page_content"],
                 template="{page_content}"
            )
            document_variable_name = "context"
            llm = OpenAI()
            # The prompt here should take as an input variable the
            # `document_variable_name`
            prompt = PromptTemplate.from_template(
                "Summarize this content: {context}"
            )
            initial_llm_chain = LLMChain(llm=llm, prompt=prompt)
            initial_response_name = "prev_response"
            # The prompt here should take as an input variable the
            # `document_variable_name` as well as `initial_response_name`
            prompt_refine = PromptTemplate.from_template(
                "Here's your first summary: {prev_response}. "
                "Now add to it based on the following context: {context}"
            )
            refine_llm_chain = LLMChain(llm=llm, prompt=prompt_refine)
            chain = RefineDocumentsChain(
                initial_llm_chain=initial_llm_chain,
                refine_llm_chain=refine_llm_chain,
                document_prompt=document_prompt,
                document_variable_name=document_variable_name,
                initial_response_name=initial_response_name,
            )
    """

    initial_llm_chain: LLMChain
    """LLM chain to use on initial document."""
    refine_llm_chain: LLMChain
    """LLM chain to use when refining."""
    document_variable_name: str
    """The variable name in the initial_llm_chain to put the documents in.
    If only one variable in the initial_llm_chain, this need not be provided."""
    initial_response_name: str
    """The variable name to format the initial response in when refining."""
    document_prompt: BasePromptTemplate = Field(
        default_factory=lambda: DEFAULT_DOCUMENT_PROMPT
    )
    """Prompt to use to format each document, gets passed to `format_document`."""
    return_intermediate_steps: bool = False
    """Return the results of the refine steps in the output."""

    @property
    def output_keys(self) -> List[str]:
        """Expect input key.

        :meta private:
        """
        _output_keys = super().output_keys
        if self.return_intermediate_steps:
            _output_keys = _output_keys + ["intermediate_steps"]
        return _output_keys

    class Config:
        """Configuration for this pydantic object."""

        extra = Extra.forbid
        arbitrary_types_allowed = True

    @root_validator(pre=True)
    def get_return_intermediate_steps(cls, values: Dict) -> Dict:
        """For backwards compatibility."""
        if "return_refine_steps" in values:
            values["return_intermediate_steps"] = values["return_refine_steps"]
            del values["return_refine_steps"]
        return values

    @root_validator(pre=True)
    def get_default_document_variable_name(cls, values: Dict) -> Dict:
        """Get default document variable name, if not provided."""
        if "document_variable_name" not in values:
            llm_chain_variables = values["initial_llm_chain"].prompt.input_variables
            if len(llm_chain_variables) == 1:
                values["document_variable_name"] = llm_chain_variables[0]
            else:
                raise ValueError(
                    "document_variable_name must be provided if there are "
                    "multiple llm_chain input_variables"
                )
        else:
            llm_chain_variables = values["initial_llm_chain"].prompt.input_variables
            if values["document_variable_name"] not in llm_chain_variables:
                raise ValueError(
                    f"document_variable_name {values['document_variable_name']} was "
                    f"not found in llm_chain input_variables: {llm_chain_variables}"
                )
        return values

    def combine_docs(
        self, docs: List[Document], callbacks: Callbacks = None, **kwargs: Any
    ) -> Tuple[str, dict]:
        """Combine by mapping first chain over all, then stuffing into final chain.

        Args:
            docs: List of documents to combine
            callbacks: Callbacks to be passed through
            **kwargs: additional parameters to be passed to LLM calls (like other
                input variables besides the documents)

        Returns:
            The first element returned is the single string output. The second
            element returned is a dictionary of other keys to return.
        """
        inputs = self._construct_initial_inputs(docs, **kwargs)
        res = self.initial_llm_chain.predict(callbacks=callbacks, **inputs)
        refine_steps = [res]
        for doc in docs[1:]:
            base_inputs = self._construct_refine_inputs(doc, res)
            inputs = {**base_inputs, **kwargs}
            res = self.refine_llm_chain.predict(callbacks=callbacks, **inputs)
            refine_steps.append(res)
        return self._construct_result(refine_steps, res)

    async def acombine_docs(
        self, docs: List[Document], callbacks: Callbacks = None, **kwargs: Any
    ) -> Tuple[str, dict]:
        """Async combine by mapping a first chain over all, then stuffing
         into a final chain.

        Args:
            docs: List of documents to combine
            callbacks: Callbacks to be passed through
            **kwargs: additional parameters to be passed to LLM calls (like other
                input variables besides the documents)

        Returns:
            The first element returned is the single string output. The second
            element returned is a dictionary of other keys to return.
        """
        inputs = self._construct_initial_inputs(docs, **kwargs)
        res = await self.initial_llm_chain.apredict(callbacks=callbacks, **inputs)
        refine_steps = [res]
        for doc in docs[1:]:
            base_inputs = self._construct_refine_inputs(doc, res)
            inputs = {**base_inputs, **kwargs}
            res = await self.refine_llm_chain.apredict(callbacks=callbacks, **inputs)
            refine_steps.append(res)
        return self._construct_result(refine_steps, res)

    def _construct_result(self, refine_steps: List[str], res: str) -> Tuple[str, dict]:
        if self.return_intermediate_steps:
            extra_return_dict = {"intermediate_steps": refine_steps}
        else:
            extra_return_dict = {}
        return res, extra_return_dict

    def _construct_refine_inputs(self, doc: Document, res: str) -> Dict[str, Any]:
        return {
            self.document_variable_name: format_document(doc, self.document_prompt),
            self.initial_response_name: res,
        }

    def _construct_initial_inputs(
        self, docs: List[Document], **kwargs: Any
    ) -> Dict[str, Any]:
        base_info = {"page_content": docs[0].page_content}
        base_info.update(docs[0].metadata)
        document_info = {k: base_info[k] for k in self.document_prompt.input_variables}
        base_inputs: dict = {
            self.document_variable_name: self.document_prompt.format(**document_info)
        }
        inputs = {**base_inputs, **kwargs}
        return inputs

    @property
    def _chain_type(self) -> str:
        return "refine_documents_chain"
