"""Check generated column reads on Elasticsearch/OpenSearch summary results."""
import ast

import pandas as pd


def _require_if_summary(obj, key):
    if not isinstance(key, str) or not isinstance(obj, (pd.DataFrame, pd.Series)):
        return
    schema = obj.attrs.get("bow_search_summary")
    if not isinstance(schema, list):
        return
    labels = obj.columns if isinstance(obj, pd.DataFrame) else obj.index
    if key in labels or len(labels) != len(schema) or list(labels) != schema:
        return  # pandas also propagates attrs to derived Series and DataFrames.
    raise ValueError(
        f"Missing search result column {key!r}. Returned columns: {list(labels[:40])!r}. "
        "For counts read doc_count and relation. Scalar metrics use their aggregation name. "
        "total_hits metadata is an object with value and relation."
    )


def _read(getter, key, *args, **kwargs):
    _require_if_summary(getattr(getter, "__self__", None), key)
    return getter(key, *args, **kwargs)


def _contains(key, obj, attribute):
    labels = obj.columns if attribute == "columns" else obj.index
    if key in labels:
        return True
    if attribute == "columns" or isinstance(obj, pd.Series):
        _require_if_summary(obj, key)
    return False


class _SearchReads(ast.NodeTransformer):
    """Keep normal Python behavior for other clients and ordinary pandas objects."""

    @staticmethod
    def _default(node):
        return isinstance(node, ast.Constant) and (node.value is None or type(node.value) in (float, int) and node.value == 0)

    def _guard_default(self, test, yes, no):
        if not isinstance(test, ast.Compare) or len(test.ops) != 1:
            return test
        labels = test.comparators[0]
        if not (isinstance(labels, ast.Attribute) and labels.attr in ("columns", "index")
                and isinstance(labels.value, ast.Name)):
            return test
        positive = isinstance(test.ops[0], ast.In)
        if not (positive or isinstance(test.ops[0], ast.NotIn)) or not self._default(no if positive else yes):
            return test
        check = ast.Call(ast.Name("_bow_contains", ast.Load()), [test.left, labels.value, ast.Constant(labels.attr)], [])
        return ast.copy_location(check if positive else ast.UnaryOp(ast.Not(), check), test)

    def visit_IfExp(self, node):
        self.generic_visit(node)
        node.test = self._guard_default(node.test, node.body, node.orelse)
        return node

    def visit_If(self, node):
        self.generic_visit(node)
        yes = getattr(node.body[0], "value", None)
        no = getattr(node.orelse[0], "value", None) if node.orelse else None
        node.test = self._guard_default(node.test, yes, no)
        return node

    def visit_Subscript(self, node):
        self.generic_visit(node)
        if not (isinstance(node.ctx, ast.Load) and (isinstance(node.slice, ast.Name)
                or isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str))):
            return node
        # Use ordinary subscription (including special-method dispatch) on every object.
        return ast.copy_location(ast.Call(ast.Name("_bow_item", ast.Load()), [node.value, node.slice], []), node)

    def visit_Call(self, node):
        self.generic_visit(node)
        if (isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args
                and not node.keywords and (len(node.args) == 1 or len(node.args) == 2 and self._default(node.args[1]))):
            node.args.insert(0, node.func)
            node.func = ast.Name("_bow_read", ast.Load())
        return node


def _item(obj, key):
    _require_if_summary(obj, key)
    return obj[key]


def exec_dataframe_code(code, namespace, *, search_summaries):
    if not search_summaries:
        exec(code, namespace)
        return
    tree = _SearchReads().visit(ast.parse(code, filename="<string>"))
    namespace.update(_bow_contains=_contains, _bow_read=_read, _bow_item=_item)
    exec(compile(ast.fix_missing_locations(tree), "<string>", "exec"), namespace)
