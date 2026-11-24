"""A bounded Python grammar, not arbitrary Python. No objects supplied by the host."""
import ast, json, math, sys
FEATURES = ('trend', 'breakout', 'oversold', 'hammer', 'pullback', 'structure', 'support', 'resistance', 'golden', 'volume', 'quiet', 'momentum')
_SAFE_BUILTINS = {}
SEED = 'def signal(' + ', '.join(FEATURES) + '):\n    return 1 if trend and momentum else 0'

def validate_source(src):
    if not isinstance(src, str) or len(src) > 2400:
        raise ValueError('source too large')
    t = ast.parse(src)
    nodes = list(ast.walk(t))
    ok = (ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.IfExp, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.Name, ast.Load, ast.Constant)
    if len(nodes) > 160 or any((type(n) not in ok for n in nodes)):
        raise ValueError('unsupported Python syntax')
    if len(t.body) != 1 or type(t.body[0]) != ast.FunctionDef:
        raise ValueError('one function required')
    f = t.body[0]
    a = f.args
    if f.name != 'signal' or f.decorator_list or f.returns or getattr(f, 'type_params', []) or a.defaults or a.kw_defaults or a.kwonlyargs or a.posonlyargs or a.vararg or a.kwarg or ([x.arg for x in a.args] != list(FEATURES)) or any((x.annotation for x in a.args)) or (len(f.body) != 1) or (type(f.body[0]) != ast.Return):
        raise ValueError('invalid signature')
    for n in nodes:
        if type(n) == ast.Name and n.id not in FEATURES:
            raise ValueError('unknown feature')
        if type(n) == ast.Constant and (type(n.value) not in (int, bool) or n.value not in (0, 1)):
            raise ValueError('invalid constant')
    return t

def compile_strategy(src):
    t = validate_source(src)
    ns = {'__builtins__': _SAFE_BUILTINS}
    exec(compile(t, '<candidate>', 'exec'), ns)
    return ns['signal']
if __name__ == '__main__':
    src, rows = json.loads(sys.stdin.buffer.read(4000001))
    fn = compile_strategy(src)
    if len(rows) > 40000 or any((len(r) != len(FEATURES) or any((type(v) != bool for v in r)) for r in rows)):
        raise ValueError('invalid features')
    print(json.dumps([int(fn(*r)) for r in rows]))
