from threading import Thread
from typing import Any, Callable, TypeVar
from mypy_extensions import (Arg, DefaultArg, NamedArg,
                             DefaultNamedArg, VarArg, KwArg)
F = TypeVar('F', bound=Callable[..., Any])

def threaded(func: F) -> Callable[[VarArg(Any), KwArg(Any)], Thread]:
    def wrapper(*args: Any, **kwargs: Any) -> Thread:
        thread = Thread(target=func, args=args, kwargs=kwargs)
        thread.start()
        return thread
    return wrapper