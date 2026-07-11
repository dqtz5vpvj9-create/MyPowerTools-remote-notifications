from typing import Generic, TypeVar

T = TypeVar('T')

class Ref(Generic[T]):
    """
    A class that wraps a reference to a value of type T.
    """

    def __init__(self, value: T) -> None:
        """
        Initializes the reference with a value of type T.

        Raises:
            ValueError: If the provided value is None.
        """
        if value is None:
            raise ValueError("Ref cannot be initialized with None")
        self._val: T = value

    @property
    def value(self) -> T:
        """
        Gets the value of the reference.
        """
        return self._val

    @value.setter
    def value(self, new_value: T) -> None:
        """
        Sets the value of the reference.
        """
        if new_value is None:
            raise ValueError("Ref value cannot be set to None")
        self._val = new_value

    def __repr__(self) -> str:
        return f"Ref({repr(self._val)})"
