"""Canonical source formatter for Flex."""

from __future__ import annotations

from flx.syntax import ast
from flx.syntax.parser import parse

_INDENT = "  "
_LAMBDA_PREFIX = "__flx_lambda_"

_INFIX_PREC: dict[str, int] = {
    "||": 2,
    "&&": 3,
    "==": 4,
    "!=": 4,
    "<": 5,
    "<=": 5,
    ">": 5,
    ">=": 5,
    "|": 6,
    "^": 7,
    "&": 8,
    "<<": 9,
    ">>": 9,
    "+": 10,
    "-": 10,
    "++": 10,
    "*": 11,
    "/": 11,
    "%": 11,
}
_UNARY_PREC = 12
_POSTFIX_PREC = 13


def format_source(source: str, file: str = "<input>") -> str:
    """Parse and return the canonical formatting of a Flex source file."""
    return format_module(parse(source, file))


def format_module(module: ast.Module) -> str:
    return _Formatter(module).module()


class _Formatter:
    def __init__(self, module: ast.Module) -> None:
        self.module_ast = module
        self.lambdas = {
            fn.name: fn
            for fn in module.functions
            if fn.name.startswith(_LAMBDA_PREFIX)
            and len(fn.body.stmts) == 1
            and isinstance(fn.body.stmts[0], ast.ExprStmt)
        }

    def module(self) -> str:
        blocks = self.module_ast.blocks or [
            ast.ModuleBlock(
                self.module_ast.name,
                self.module_ast.imports,
                self.module_ast.items,
                self.module_ast.span,
                self.module_ast.import_spans,
                self.module_ast.import_decls,
            )
        ]
        chunks = [self._module_block(block) for block in blocks]
        return "\n\n".join(chunks).rstrip() + "\n"

    def _module_block(self, block: ast.ModuleBlock) -> str:
        lines = [f"module {block.name} {{"]
        import_decls = block.import_decls or [
            ast.ImportDecl(module=module, span=span)
            for module, span in zip(
                block.imports,
                [*block.import_spans, block.span],
                strict=False,
            )
        ]
        for decl in import_decls:
            lines.append(f"{self._ind(1)}import {self._import_decl(decl)}")

        items = [item for item in block.items if not self._is_synthetic_lambda(item)]
        if import_decls and items:
            lines.append("")
        for i, item in enumerate(items):
            if i > 0:
                lines.append("")
            self._append_with_indent(lines, self._item(item, 1), 1)
        lines.append("}")
        return "\n".join(lines)

    @staticmethod
    def _is_synthetic_lambda(item: ast.Item) -> bool:
        return isinstance(item, ast.FnDecl) and item.name.startswith(_LAMBDA_PREFIX)

    @staticmethod
    def _import_decl(decl: ast.ImportDecl) -> str:
        if decl.names is not None:
            return f"{decl.module}.{{{', '.join(decl.names)}}}"
        if decl.alias is not None:
            return f"{decl.module} as {decl.alias}"
        return decl.module

    @staticmethod
    def _ind(level: int) -> str:
        return _INDENT * level

    def _append_with_indent(self, out: list[str], text: str, level: int) -> None:
        lines = text.splitlines()
        if not lines:
            return
        out.append(f"{self._ind(level)}{lines[0]}")
        out.extend(lines[1:])

    def _item(self, item: ast.Item, indent: int) -> str:
        pub = "pub " if getattr(item, "pub", False) else ""
        if isinstance(item, ast.FnDecl):
            params = ", ".join(self._param(p) for p in item.params)
            tps = self._fn_type_params(item.type_params)
            ret = f" -> {self._type(item.return_type)}" if item.return_type else ""
            return (
                f"{pub}fn {item.name}{tps}({params}){ret}"
                f"{self._uses(item.effects)} = {self._block(item.body, indent)}"
            )
        if isinstance(item, ast.ExternFnDecl):
            params = ", ".join(self._param(p) for p in item.params)
            ret = f" -> {self._type(item.return_type)}" if item.return_type else ""
            return f"{pub}extern fn {item.name}({params}){ret}{self._uses(item.effects)}"
        if isinstance(item, ast.TestDecl):
            return (
                f"test {self._string(item.name)}{self._uses(item.effects)} "
                f"{self._block(item.body, indent)}"
            )
        if isinstance(item, ast.RecordDecl):
            return self._record_decl(item, indent, pub)
        if isinstance(item, ast.AdtDecl):
            params = self._type_param_names(item.type_params)
            variants = " | ".join(self._variant(v) for v in item.variants)
            return f"{pub}{self._derives(item.derives)}type {item.name}{params} = | {variants}"
        if isinstance(item, ast.MacroDecl):
            return f"macro {item.name}({', '.join(item.params)}) = {self._expr(item.body, indent)}"
        if isinstance(item, ast.TraitDecl):
            lines = [f"{pub}trait {item.name} = {{"]
            for trait_method in item.methods:
                params = ", ".join(self._param(p) for p in trait_method.params)
                ret = (
                    f" -> {self._type(trait_method.return_type)}"
                    if trait_method.return_type
                    else ""
                )
                lines.append(f"{self._ind(indent + 1)}fn {trait_method.name}({params}){ret}")
            lines.append(f"{self._ind(indent)}}}")
            return "\n".join(lines)
        if isinstance(item, ast.ImplDecl):
            lines = [f"impl {item.trait} for {item.type_name} = {{"]
            for i, impl_method in enumerate(item.methods):
                if i > 0:
                    lines.append("")
                self._append_with_indent(lines, self._item(impl_method, indent + 1), indent + 1)
            lines.append(f"{self._ind(indent)}}}")
            return "\n".join(lines)
        if isinstance(item, ast.TargetDecl):
            return f"target {item.name}{self._uses(item.effects)} {self._block(item.body, indent)}"
        if isinstance(item, ast.DefaultTargetDecl):
            return f"target default = {item.name}"
        if isinstance(item, ast.DocDecl):
            return self._doc_decl(item, indent)
        raise TypeError(f"unhandled item {type(item).__name__}")

    def _record_decl(self, item: ast.RecordDecl, indent: int, pub: str) -> str:
        params = self._type_param_names(item.type_params)
        lines = [f"{pub}{self._derives(item.derives)}type {item.name}{params} = {{"]
        for field in item.fields:
            lines.append(f"{self._ind(indent + 1)}{field.name}: {self._type(field.type)}")
        lines.append(f"{self._ind(indent)}}}")
        return "\n".join(lines)

    @staticmethod
    def _type_param_names(names: list[str]) -> str:
        return f"<{', '.join(names)}>" if names else ""

    def _fn_type_params(self, params: list[ast.TypeParam]) -> str:
        if not params:
            return ""
        rendered = []
        for param in params:
            bounds = f": {' + '.join(param.bounds)}" if param.bounds else ""
            rendered.append(f"{param.name}{bounds}")
        return f"<{', '.join(rendered)}>"

    def _derives(self, derives: list[str]) -> str:
        return f"derive({', '.join(derives)}) " if derives else ""

    def _doc_decl(self, item: ast.DocDecl, indent: int) -> str:
        head = self._string(item.title) if item.title is not None else item.target or "module"
        lines = [f"doc {head} {{"]
        if item.summary is not None:
            lines.append(f"{self._ind(indent + 1)}summary {self._string(item.summary)}")
        if item.slug is not None:
            lines.append(f"{self._ind(indent + 1)}slug {self._string(item.slug)}")
        if item.since is not None:
            lines.append(f"{self._ind(indent + 1)}since {self._string(item.since)}")
        if item.status is not None:
            lines.append(f"{self._ind(indent + 1)}status {item.status}")
        for see in item.sees:
            lines.append(f"{self._ind(indent + 1)}see {see}")
        for entry in item.content:
            if isinstance(entry, ast.DocText):
                self._doc_text(lines, entry, indent + 1)
            elif isinstance(entry, ast.DocSnippet):
                lines.append(f"{self._ind(indent + 1)}snippet {self._string(entry.name)} {{")
                self._append_raw(lines, entry.source, indent + 2)
                lines.append(f"{self._ind(indent + 1)}}}")
            elif isinstance(entry, ast.DocTest):
                self._doc_test(lines, entry, indent + 1)
        lines.append(f"{self._ind(indent)}}}")
        return "\n".join(lines)

    def _doc_text(self, lines: list[str], entry: ast.DocText, indent: int) -> None:
        if "\n" not in entry.text and '"""' not in entry.text:
            lines.append(f"{self._ind(indent)}text {self._string(entry.text)}")
            return
        lines.append(f'{self._ind(indent)}text """')
        self._append_raw(lines, entry.text, indent + 1)
        lines.append(f'{self._ind(indent)}"""')

    def _doc_test(self, lines: list[str], entry: ast.DocTest, indent: int) -> None:
        expect = f" expect_error {entry.expect_error}" if entry.expect_error else ""
        head = f"test {self._string(entry.name)}{expect}{self._uses(entry.effects)}"
        if entry.expect_error is not None:
            lines.append(f"{self._ind(indent)}{head} {{")
            self._append_raw(lines, entry.source, indent + 1)
            lines.append(f"{self._ind(indent)}}}")
        elif entry.body is not None:
            lines.append(f"{self._ind(indent)}{head} {self._block(entry.body, indent)}")

    def _append_raw(self, lines: list[str], source: str, indent: int) -> None:
        for line in source.splitlines():
            lines.append(f"{self._ind(indent)}{line}" if line else "")

    def _param(self, param: ast.Param) -> str:
        return f"{param.name}: {self._type(param.type)}"

    def _type(self, ty: ast.TypeExpr | None) -> str:
        if ty is None:
            return "Unit"
        if ty.name == "->" and ty.args:
            params = ", ".join(self._type(arg) for arg in ty.args[:-1])
            return f"({params}) -> {self._type(ty.args[-1])}"
        if ty.args:
            return f"{ty.name}<{', '.join(self._type(arg) for arg in ty.args)}>"
        return ty.name

    def _uses(self, effects: list[str]) -> str:
        return f" uses {{ {', '.join(effects)} }}" if effects else ""

    def _variant(self, variant: ast.Variant) -> str:
        if not variant.payload:
            return variant.name
        return f"{variant.name}({', '.join(self._type(ty) for ty in variant.payload)})"

    def _block(self, block: ast.Block, indent: int) -> str:
        if not block.stmts:
            return "{}"
        lines = ["{"]
        for stmt in block.stmts:
            rendered = self._stmt(stmt, indent + 1)
            stmt_lines = rendered.splitlines()
            lines.append(f"{self._ind(indent + 1)}{stmt_lines[0]}")
            lines.extend(stmt_lines[1:])
        lines.append(f"{self._ind(indent)}}}")
        return "\n".join(lines)

    def _stmt(self, stmt: ast.Stmt, indent: int) -> str:
        if isinstance(stmt, ast.LetStmt):
            annotation = f": {self._type(stmt.annotation)}" if stmt.annotation else ""
            return f"let {stmt.name}{annotation} = {self._expr(stmt.value, indent)}"
        if isinstance(stmt, ast.MutStmt):
            annotation = f": {self._type(stmt.annotation)}" if stmt.annotation else ""
            return f"mut {stmt.name}{annotation} = {self._expr(stmt.value, indent)}"
        if isinstance(stmt, ast.AssignStmt):
            return f"{stmt.name} = {self._expr(stmt.value, indent)}"
        if isinstance(stmt, ast.IndexAssignStmt):
            obj = self._expr(stmt.obj, indent, _POSTFIX_PREC)
            index = self._expr(stmt.index, indent)
            return f"{obj}[{index}] = {self._expr(stmt.value, indent)}"
        if isinstance(stmt, ast.WhileStmt):
            return f"while {self._expr(stmt.cond, indent)} {self._block(stmt.body, indent)}"
        if isinstance(stmt, ast.ForStmt):
            return (
                f"for {stmt.name} in {self._expr(stmt.iter, indent)} "
                f"{self._block(stmt.body, indent)}"
            )
        if isinstance(stmt, ast.ReturnStmt):
            if stmt.value is None:
                return "return"
            return f"return {self._expr(stmt.value, indent)}"
        if isinstance(stmt, ast.ExprStmt):
            return self._expr(stmt.expr, indent)
        raise TypeError(f"unhandled statement {type(stmt).__name__}")

    def _expr(self, expr: ast.Expr, indent: int, parent_prec: int = 0) -> str:
        if isinstance(expr, ast.IntLit):
            return str(expr.value)
        if isinstance(expr, ast.FloatLit):
            return format(expr.value, ".17g")
        if isinstance(expr, ast.BoolLit):
            return "true" if expr.value else "false"
        if isinstance(expr, ast.StringLit):
            return self._string(expr.value)
        if isinstance(expr, ast.BytesLit):
            return f"<<{', '.join(self._expr(part, indent) for part in expr.parts)}>>"
        if isinstance(expr, ast.NameExpr):
            if expr.name in self.lambdas:
                return self._lambda_expr(self.lambdas[expr.name], indent)
            return expr.name
        if isinstance(expr, ast.MemberExpr):
            text = f"{self._expr(expr.obj, indent, _POSTFIX_PREC)}.{expr.name}"
            return self._maybe_paren(text, _POSTFIX_PREC, parent_prec)
        if isinstance(expr, ast.IndexExpr):
            text = (
                f"{self._expr(expr.obj, indent, _POSTFIX_PREC)}"
                f"[{self._expr(expr.index, indent)}]"
            )
            return self._maybe_paren(text, _POSTFIX_PREC, parent_prec)
        if isinstance(expr, ast.UnaryExpr):
            text = f"{expr.op}{self._expr(expr.operand, indent, _UNARY_PREC)}"
            return self._maybe_paren(text, _UNARY_PREC, parent_prec)
        if isinstance(expr, ast.BinaryExpr):
            prec = _INFIX_PREC[expr.op]
            left = self._expr(expr.left, indent, prec)
            right = self._expr(expr.right, indent, prec + 1)
            text = f"{left} {expr.op} {right}"
            return self._maybe_paren(text, prec, parent_prec)
        if isinstance(expr, ast.CallExpr):
            args = ", ".join(self._expr(arg, indent) for arg in expr.args)
            text = f"{self._expr(expr.callee, indent, _POSTFIX_PREC)}({args})"
            return self._maybe_paren(text, _POSTFIX_PREC, parent_prec)
        if isinstance(expr, ast.IfExpr):
            return self._maybe_paren(self._if_expr(expr, indent), 1, parent_prec)
        if isinstance(expr, ast.UnitLit):
            return "()"
        if isinstance(expr, ast.ListExpr):
            return f"[{', '.join(self._expr(item, indent) for item in expr.items)}]"
        if isinstance(expr, ast.RecordExpr):
            return self._record_expr(expr, indent)
        if isinstance(expr, ast.RecordUpdateExpr):
            fields = ", ".join(self._field_init(field, indent) for field in expr.fields)
            return f"{{ {self._expr(expr.base, indent)} with {fields} }}"
        if isinstance(expr, ast.RegionExpr):
            return f"region {expr.name} {self._block(expr.body, indent)}"
        if isinstance(expr, ast.TryExpr):
            text = f"{self._expr(expr.expr, indent, _POSTFIX_PREC)}?"
            return self._maybe_paren(text, _POSTFIX_PREC, parent_prec)
        if isinstance(expr, ast.ComptimeExpr):
            return f"comptime {self._block(expr.body, indent)}"
        if isinstance(expr, ast.QuoteExpr):
            return f"quote {self._block(expr.body, indent)}"
        if isinstance(expr, ast.UnquoteExpr):
            return f"unquote({self._expr(expr.expr, indent)})"
        if isinstance(expr, ast.UnquoteSpliceExpr):
            return f"unquote_splice({self._expr(expr.expr, indent)})"
        if isinstance(expr, ast.MatchExpr):
            return self._maybe_paren(self._match_expr(expr, indent), 1, parent_prec)
        if isinstance(expr, ast.BlockExpr):
            return self._block(expr.body, indent)
        raise TypeError(f"unhandled expression {type(expr).__name__}")

    def _lambda_expr(self, fn: ast.FnDecl, indent: int) -> str:
        params = ", ".join(self._param(param) for param in fn.params)
        body = fn.body.tail
        if body is None:
            return fn.name
        return f"fn({params}) -> {self._type(fn.return_type)} => {self._expr(body, indent)}"

    @staticmethod
    def _maybe_paren(text: str, prec: int, parent_prec: int) -> str:
        return f"({text})" if prec < parent_prec else text

    def _if_expr(self, expr: ast.IfExpr, indent: int) -> str:
        text = f"if {self._expr(expr.cond, indent)} {self._block(expr.then_block, indent)}"
        if expr.else_block is None:
            return text
        if (
            len(expr.else_block.stmts) == 1
            and isinstance(expr.else_block.stmts[0], ast.ExprStmt)
            and isinstance(expr.else_block.stmts[0].expr, ast.IfExpr)
        ):
            inner = self._if_expr(expr.else_block.stmts[0].expr, indent)
            return f"{text} else {inner}"
        return f"{text} else {self._block(expr.else_block, indent)}"

    def _match_expr(self, expr: ast.MatchExpr, indent: int) -> str:
        lines = [f"match {self._expr(expr.scrutinee, indent)} {{"]
        for arm in expr.arms:
            body = self._expr(arm.body, indent + 1)
            body_lines = body.splitlines()
            lines.append(
                f"{self._ind(indent + 1)}{self._pattern(arm.pattern)} => {body_lines[0]}"
            )
            lines.extend(body_lines[1:])
        lines.append(f"{self._ind(indent)}}}")
        return "\n".join(lines)

    def _record_expr(self, expr: ast.RecordExpr, indent: int) -> str:
        if not expr.fields:
            return "{}"
        fields = ", ".join(self._field_init(field, indent) for field in expr.fields)
        trailing = "," if len(expr.fields) == 1 else ""
        return f"{{ {fields}{trailing} }}"

    def _field_init(self, field: ast.FieldInit, indent: int) -> str:
        return f"{field.name} = {self._expr(field.value, indent)}"

    def _pattern(self, pattern: ast.Pattern) -> str:
        if isinstance(pattern, ast.WildcardPattern):
            return "_"
        if isinstance(pattern, ast.BindPattern):
            return pattern.name
        if isinstance(pattern, ast.CtorPattern):
            if pattern.args:
                return f"{pattern.name}({', '.join(self._pattern(arg) for arg in pattern.args)})"
            return pattern.name
        if isinstance(pattern, ast.LiteralPattern):
            if isinstance(pattern.value, bool):
                return "true" if pattern.value else "false"
            return str(pattern.value)
        raise TypeError(f"unhandled pattern {type(pattern).__name__}")

    def _string(self, value: str) -> str:
        out = ['"']
        for ch in value:
            if ch == "\\":
                out.append("\\\\")
            elif ch == '"':
                out.append('\\"')
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\t":
                out.append("\\t")
            elif ch == "\r":
                out.append("\\r")
            elif 0x20 <= ord(ch) <= 0x7E:
                out.append(ch)
            else:
                for byte in ch.encode("utf-8", "surrogateescape"):
                    out.append(f"\\x{byte:02x}")
        out.append('"')
        return "".join(out)
