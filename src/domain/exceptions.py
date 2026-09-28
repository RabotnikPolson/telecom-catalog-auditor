class DomainError(Exception):
    pass


class ProductValidationError(DomainError):
    pass


class DuplicateProductError(DomainError):
    pass


class ProductNotFoundError(DomainError):
    pass


class GroupingError(DomainError):
    pass
