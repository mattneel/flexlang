"""Render the AST as a stable, indented tree for `flx parse` and snapshots."""

from __future__ import annotations

from flx.syntax import ast


def dump_module(module: ast.Module) -> str:
    lines: list[str] = [f"Module {module.name}"]
    for imp in module.imports:
        lines.append(f"  import {imp}")
    for item in module.items:
        _dump_item(item, 1, lines)
    return "\n".join(lines)


def _indent(depth: int) -> str:
    return "  " * depth


def _dump_item(item: ast.Item, depth: int, out: list[str]) -> None:
    if isinstance(item, ast.FnDecl):
        params = ", ".join(f"{p.name}: {_type(p.type)}" for p in item.params)
        ret = _type(item.return_type) if item.return_type else "Unit"
        uses = f" uses {{{', '.join(item.effects)}}}" if item.effects else ""
        out.append(f"{_indent(depth)}Fn {item.name}({params}) -> {ret}{uses}")
        _dump_block(item.body, depth + 1, out)
    elif isinstance(item, ast.TestDecl):
        uses = f" uses {{{', '.join(item.effects)}}}" if item.effects else ""
        out.append(f"{_indent(depth)}Test {item.name!r}{uses}")
        _dump_block(item.body, depth + 1, out)


def _type(t: ast.TypeExpr) -> str:
    if t.args:
        return f"{t.name}<{', '.join(_type(a) for a in t.args)}>"
    return t.name


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
    elif isinstance(stmt, ast.WhileStmt):
        out.append(f"{pad}While {_expr(stmt.cond)}")
        _dump_block(stmt.body, depth + 1, out)
    elif isinstance(stmt, ast.ReturnStmt):
        out.append(f"{pad}Return {_expr(stmt.value) if stmt.value else ''}".rstrip())
    elif isinstance(stmt, ast.ExprStmt):
        out.append(f"{pad}Expr {_expr(stmt.expr)}")


def _expr(expr: ast.Expr) -> str:
    if isinstance(expr, ast.IntLit):
        return str(expr.value)
    if isinstance(expr, ast.BoolLit):
        return "true" if expr.value else "false"
    if isinstance(expr, ast.StringLit):
        return repr(expr.value)
    if isinstance(expr, ast.NameExpr):
        return expr.name
    if isinstance(expr, ast.MemberExpr):
        return f"{_expr(expr.obj)}.{expr.name}"
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
    return "<expr>"
