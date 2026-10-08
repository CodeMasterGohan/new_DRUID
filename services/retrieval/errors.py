"""Retrieval service errors. All failures are fail-closed."""


class RetrievalError(Exception):
    """Base class for retrieval failures."""


class ContextValidationError(RetrievalError):
    """Caller context is missing or invalid — fail closed, return nothing."""


class AuthorizationError(RetrievalError):
    """An authorization predicate or catalog check rejected the request."""


class DependencyError(RetrievalError):
    """An injected collaborator (embedding, vector store, catalog, reranker)
    failed or returned a contract-violating response."""
