"""Render the AST as a stable, indented tree for `flx parse` and snapshots."""

from __future__ import annotations

from flx.syntax import ast


def dump_module(module: ast.Module) -> str:
    lines: list[str] = [f"Module {module.name}"]
    if module.import_decls:
        for decl in module.import_decls:
            lines.append(f"  import {_import_decl(decl)}")
    else:
        for imp in module.imports:
            lines.append(f"  import {imp}")
    for item in module.items:
        _dump_item(item, 1, lines)
    return "\n".join(lines)


def _import_decl(decl: ast.ImportDecl) -> str:
    if decl.names is not None:
        return f"{decl.module}.{{{', '.join(decl.names)}}}"
    if decl.alias is not None:
        return f"{decl.module} as {decl.alias}"
    return decl.module


def _indent(depth: int) -> str:
    return "  " * depth


def _dump_item(item: ast.Item, depth: int, out: list[str]) -> None:
    if isinstance(item, ast.FnDecl):
        params = ", ".join(f"{p.name}: {_type(p.type)}" for p in item.params)
        ret = _type(item.return_type) if item.return_type else "Unit"
        uses = f" uses {{{', '.join(item.effects)}}}" if item.effects else ""
        tps = ""
        if item.type_params:
            tps = "<" + ", ".join(_type_param(tp) for tp in item.type_params) + ">"
        out.append(f"{_indent(depth)}Fn {item.name}{tps}({params}) -> {ret}{uses}")
        _dump_block(item.body, depth + 1, out)
    elif isinstance(item, ast.TraitDecl):
        out.append(f"{_indent(depth)}Trait {item.name}")
        for sig in item.methods:
            mp = ", ".join(f"{p.name}: {_type(p.type)}" for p in sig.params)
            mr = _type(sig.return_type) if sig.return_type else "Unit"
            out.append(f"{_indent(depth + 1)}fn {sig.name}({mp}) -> {mr}")
    elif isinstance(item, ast.ImplDecl):
        out.append(f"{_indent(depth)}Impl {item.trait} for {item.type_name}")
        for method in item.methods:
            _dump_item(method, depth + 1, out)
    elif isinstance(item, ast.TestDecl):
        uses = f" uses {{{', '.join(item.effects)}}}" if item.effects else ""
        out.append(f"{_indent(depth)}Test {item.name!r}{uses}")
        _dump_block(item.body, depth + 1, out)
    elif isinstance(item, ast.RecordDecl):
        params = f"<{', '.join(item.type_params)}>" if item.type_params else ""
        fields = ", ".join(f"{f.name}: {_type(f.type)}" for f in item.fields)
        derives = f" derive({', '.join(item.derives)})" if item.derives else ""
        out.append(f"{_indent(depth)}Record {item.name}{params} {{{fields}}}{derives}")
    elif isinstance(item, ast.AdtDecl):
        params = f"<{', '.join(item.type_params)}>" if item.type_params else ""
        variants = " | ".join(
            v.name + (f"({', '.join(_type(t) for t in v.payload)})" if v.payload else "")
            for v in item.variants
        )
        derives = f" derive({', '.join(item.derives)})" if item.derives else ""
        out.append(f"{_indent(depth)}Adt {item.name}{params} = {variants}{derives}")
    elif isinstance(item, ast.MacroDecl):
        params = ", ".join(item.params)
        out.append(f"{_indent(depth)}Macro {item.name}({params}) = {_expr(item.body)}")
    elif isinstance(item, ast.ExternFnDecl):
        params = ", ".join(f"{p.name}: {_type(p.type)}" for p in item.params)
        ret = _type(item.return_type) if item.return_type else "Unit"
        uses = f" uses {{{', '.join(item.effects)}}}" if item.effects else ""
        out.append(f"{_indent(depth)}Extern fn {item.name}({params}) -> {ret}{uses}")
    elif isinstance(item, ast.TargetDecl):
        uses = f" uses {{{', '.join(item.effects)}}}" if item.effects else ""
        out.append(f"{_indent(depth)}Target {item.name}{uses}")
        _dump_block(item.body, depth + 1, out)
    elif isinstance(item, ast.DefaultTargetDecl):
        out.append(f"{_indent(depth)}Target default = {item.name}")
    elif isinstance(item, ast.DocDecl):
        head = item.title if item.title is not None else item.target
        out.append(f"{_indent(depth)}Doc {head!r}")
        if item.summary:
            out.append(f"{_indent(depth + 1)}summary {item.summary!r}")
        for entry in item.content:
            if isinstance(entry, ast.DocText):
                out.append(f"{_indent(depth + 1)}text ({len(entry.text)} chars)")
            elif isinstance(entry, ast.DocTest):
                tag = f" expect_error {entry.expect_error}" if entry.expect_error else ""
                out.append(f"{_indent(depth + 1)}test {entry.name!r}{tag}")
            elif isinstance(entry, ast.DocSnippet):
                out.append(f"{_indent(depth + 1)}snippet {entry.name!r}")


def _type(t: ast.TypeExpr) -> str:
    if t.args:
        return f"{t.name}<{', '.join(_type(a) for a in t.args)}>"
    return t.name


def _type_param(tp: ast.TypeParam) -> str:
    return f"{tp.name}: {' + '.join(tp.bounds)}" if tp.bounds else tp.name


def _dump_block(block: ast.Block, depth: int, out: list[str]) -> None:
    out.append(f"{_indent(depth)}Block")
    for stmt in block.stmts:
        _dump_stmt(stmt, depth + 1, out)


def _dump_stmt(stmt: ast.Stmt, depth: int, out: list[str]) -> None:
    pad = _indent(depth)
    if isinstance(stmt, ast.LetStmt):
        out.append(f"{pad}Let {stmt.name} = {_expr(stmt.value)}")
    elif isinstance(stmt, ast.MutStmt):
        out.append(f"{pad}Mut {stmt.name} = {_expr(stmt.value)}")
    elif isinstance(stmt, ast.AssignStmt):
        out.append(f"{pad}Assign {stmt.name} = {_expr(stmt.value)}")
    elif isinstance(stmt, ast.IndexAssignStmt):
        out.append(f"{pad}IndexAssign {_expr(stmt.obj)}[{_expr(stmt.index)}] = {_expr(stmt.value)}")
    elif isinstance(stmt, ast.WhileStmt):
        out.append(f"{pad}While {_expr(stmt.cond)}")
        _dump_block(stmt.body, depth + 1, out)
    elif isinstance(stmt, ast.ForStmt):
        out.append(f"{pad}For {stmt.name} in {_expr(stmt.iter)}")
        _dump_block(stmt.body, depth + 1, out)
    elif isinstance(stmt, ast.ReturnStmt):
        out.append(f"{pad}Return {_expr(stmt.value) if stmt.value else ''}".rstrip())
    elif isinstance(stmt, ast.ExprStmt):
        out.append(f"{pad}Expr {_expr(stmt.expr)}")


def _expr(expr: ast.Expr) -> str:
    if isinstance(expr, ast.IntLit):
        return str(expr.value)
    if isinstance(expr, ast.FloatLit):
        return repr(expr.value)
    if isinstance(expr, ast.BoolLit):
        return "true" if expr.value else "false"
    if isinstance(expr, ast.StringLit):
        return repr(expr.value)
    if isinstance(expr, ast.NameExpr):
        return expr.name
    if isinstance(expr, ast.MemberExpr):
        return f"{_expr(expr.obj)}.{expr.name}"
    if isinstance(expr, ast.IndexExpr):
        return f"{_expr(expr.obj)}[{_expr(expr.index)}]"
    if isinstance(expr, ast.UnaryExpr):
        return f"({expr.op} {_expr(expr.operand)})"
    if isinstance(expr, ast.BinaryExpr):
        return f"({_expr(expr.left)} {expr.op} {_expr(expr.right)})"
    if isinstance(expr, ast.CallExpr):
        args = ", ".join(_expr(a) for a in expr.args)
        return f"{_expr(expr.callee)}({args})"
    if isinstance(expr, ast.IfExpr):
        base = f"if {_expr(expr.cond)} {{...}}"
        return base + " else {...}" if expr.else_block else base
    if isinstance(expr, ast.UnitLit):
        return "()"
    if isinstance(expr, ast.ListExpr):
        return "[" + ", ".join(_expr(i) for i in expr.items) + "]"
    if isinstance(expr, ast.RecordExpr):
        return "{" + ", ".join(f"{f.name} = {_expr(f.value)}" for f in expr.fields) + "}"
    if isinstance(expr, ast.RecordUpdateExpr):
        fields = ", ".join(f"{f.name} = {_expr(f.value)}" for f in expr.fields)
        return f"{{{_expr(expr.base)} with {fields}}}"
    if isinstance(expr, ast.TryExpr):
        return f"{_expr(expr.expr)}?"
    if isinstance(expr, ast.RegionExpr):
        return f"region {expr.name} {{...}}"
    if isinstance(expr, ast.MatchExpr):
        arms = " ".join(f"{_pattern(a.pattern)} => {_expr(a.body)}" for a in expr.arms)
        return f"match {_expr(expr.scrutinee)} {{ {arms} }}"
    if isinstance(expr, ast.BlockExpr):
        if len(expr.body.stmts) > 1:
            return f"{{ ...; {_block_value(expr.body)} }}"  # statements elided
        return f"{{ {_block_value(expr.body)} }}"
    if isinstance(expr, ast.ComptimeExpr):
        return f"comptime {{ {_block_value(expr.body)} }}"
    if isinstance(expr, ast.QuoteExpr):
        return f"quote {{ {_block_value(expr.body)} }}"
    if isinstance(expr, ast.UnquoteExpr):
        return f"unquote({_expr(expr.expr)})"
    if isinstance(expr, ast.UnquoteSpliceExpr):
        return f"unquote_splice({_expr(expr.expr)})"
    return "<expr>"


def _block_value(block: ast.Block) -> str:
    tail = block.tail
    return _expr(tail) if tail is not None else "..."


def _pattern(pat: ast.Pattern) -> str:
    if isinstance(pat, ast.WildcardPattern):
        return "_"
    if isinstance(pat, ast.BindPattern):
        return pat.name
    if isinstance(pat, ast.CtorPattern):
        if pat.args:
            return f"{pat.name}({', '.join(_pattern(a) for a in pat.args)})"
        return pat.name
    if isinstance(pat, ast.LiteralPattern):
        return "true" if pat.value is True else "false" if pat.value is False else str(pat.value)
    return "<pat>"
