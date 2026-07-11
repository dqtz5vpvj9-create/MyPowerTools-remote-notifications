import typing

class StrRef:
    """
    一个可变的包装类，用于持有字符串值，模拟“字符串引用”的行为。

    由于 Python 的 str 是不可变的，这个类允许你改变实例所持有的
    字符串对象，从而让所有引用该实例的地方都能看到变化。
    """
    # __slots__ 可以略微优化内存使用，并防止添加任意新属性
    __slots__ = ('_value',)

    def __init__(self, initial_value: str = ""):
        """
        初始化 StrRef 实例。

        Args:
            initial_value: 初始的字符串值。默认为空字符串。

        Raises:
            TypeError: 如果 initial_value 不是字符串。
        """
        if not isinstance(initial_value, str):
            raise TypeError(f"Initial value must be a str, not {type(initial_value).__name__}")
        self._value: str = initial_value

    @property
    def value(self) -> str:
        """获取当前持有的字符串值。"""
        return self._value

    @value.setter
    def value(self, new_value: str):
        """设置一个新的字符串值。"""
        if not isinstance(new_value, str):
            raise TypeError(f"Value must be a str, not {type(new_value).__name__}")
        self._value = new_value

    # --- 让它表现得更像字符串一点 ---

    def __str__(self) -> str:
        """返回字符串表示（即内部持有的字符串）。"""
        return self._value

    def __repr__(self) -> str:
        """返回对象的详细表示，便于调试。"""
        # 使用 repr() 处理内部字符串的引号等
        return f"{self.__class__.__name__}({repr(self._value)})"

    def __len__(self) -> int:
        """返回内部字符串的长度。"""
        return len(self._value)

    def __eq__(self, other) -> bool:
        """
        比较 StrRef 对象或与字符串的相等性。
        比较的是内部持有的字符串值。
        """
        if isinstance(other, StrRef):
            return self._value == other._value
        if isinstance(other, str):
            return self._value == other
        # 对于其他类型，返回 NotImplemented 以允许对方尝试比较
        return NotImplemented

    def __ne__(self, other) -> bool:
        """不等于比较。"""
        result = self.__eq__(other)
        return NotImplemented if result is NotImplemented else not result

    # --- 哈希 ---
    # 因为 StrRef 是可变的，它通常不应该被用作字典键或集合元素。
    # 将 __hash__ 设置为 None 明确表示它是不可哈希的。
    __hash__ = None

if __name__ == "__main__":
    # --- 使用示例 ---

    def process_string(s_ref: StrRef):
        """一个函数，它接收 StrRef 并修改其内容。"""
        print(f"  [Inside Function] Received ref: {s_ref!r}, value: '{s_ref.value}'")
        # 修改 StrRef 持有的字符串
        s_ref.value = s_ref.value.upper() + " (modified)"
        print(f"  [Inside Function] Modified ref: {s_ref!r}, value: '{s_ref.value}'")

    # 创建一个 StrRef 实例
    my_string_ref = StrRef("hello world")
    print(f"[Outside Function] Initial ref: {my_string_ref!r}, value: '{my_string_ref.value}'")
    print(f"Length: {len(my_string_ref)}")

    # 将 StrRef 实例传递给函数
    process_string(my_string_ref)

    # 检查函数外部的 StrRef 实例，它的值已经被改变了
    print(f"[Outside Function] After call ref: {my_string_ref!r}, value: '{my_string_ref.value}'")
    print(f"Length: {len(my_string_ref)}")

    # 比较
    ref2 = StrRef("HELLO WORLD (modified)")
    print(f"Comparison (ref == str): {my_string_ref == 'HELLO WORLD (modified)'}") # True
    print(f"Comparison (ref == ref2): {my_string_ref == ref2}")             # True
    print(f"Comparison (ref != 'something else'): {my_string_ref != 'something else'}") # True

    # 尝试设置非字符串值
    try:
        my_string_ref.value = 123
    except TypeError as e:
        print(f"Caught expected error: {e}")

    # 尝试将其用作字典键 (会失败，因为 __hash__ = None)
    # try:
    #     my_dict = {my_string_ref: "some data"}
    # except TypeError as e:
    #     print(f"Caught expected error when hashing: {e}")