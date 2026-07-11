from pylint.checkers import BaseChecker
from pylint.interfaces import IAstroidChecker


class InitDeclarationChecker(BaseChecker):
    __implements__ = IAstroidChecker

    name = 'init-declaration-checker'
    priority = -1
    msgs = {
        'E9999': (
            'Instance variable "%s" not declared in __init__',
            'not-in-init',
            'Used when an instance variable is defined outside __init__',
        ),
    }

    def visit_assignname(self, node):
        if (
            'self.' in node.as_string()
            and not node.frame().name == '__init__'
            and node.root().name != '__init__'
        ):
            self.add_message(
                'not-in-init',
                node=node,
                args=(node.as_string(),),
            )


def register(linter):
    linter.register_checker(InitDeclarationChecker(linter))
