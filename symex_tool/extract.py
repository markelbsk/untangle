import logging
from typing import Dict
from collections import defaultdict, deque, namedtuple

from .codeql import run_codeql_query
from .utils import save_object, restore_object
from .variable import Variable, Pointer


logger = logging.getLogger('extract')

Struct      = namedtuple('Struct', ('size', 'fields'))
StructField = namedtuple('StructField', ('name', 'type', 'offset', 'size'))


def parse_struct_ptr(name: str, structs: Dict[str,Struct]) -> Pointer:
    if name not in structs:
        return None

    logger.debug('Recursively parsing struct %r', name)
    root = Pointer(name, structs[name].size, {})
    q = deque([root])
    parent = {}

    while q:
        cur = q.popleft()

        for f in structs[cur.name].fields:
            # Cannot parse these further
            if f.type in ('struct <unnamed>', 'union <unnamed>'):
                cur.fields[f.offset] = f.size
                continue

            fname, ftype, foff, fsize = f
            array_sz = 1

            # Detect array members
            if ftype.endswith(']'):
                start = ftype.rfind('[')
                array_sz = int(ftype[start + 1:-1])
                ftype = ftype[:start]
                fsize //= array_sz

            if ftype.endswith('*'):
                ftype = ftype[:-1].strip()

                # Cannot handle double pointers
                if not ftype.endswith('*'):
                    # Another struct pointer?
                    if ftype not in PRIMITIVE_TYPES:
                        inf = False

                        # Avoid infinite loops
                        if ftype == cur.name:
                            inf = True
                        else:
                            par = ftype
                            seen = set(par)

                            while par in parent:
                                newpar = parent[par]
                                if newpar == ftype or newpar in seen:
                                    inf = True
                                    break

                                if newpar == par:
                                    break

                                par = newpar
                                seen.add(par)

                        if not inf:
                            parent[ftype] = cur.name

                            for i in range(array_sz):
                                if ftype in structs:
                                    p = Pointer(ftype, structs[ftype].size, {})
                                    cur.fields[foff + i * fsize] = p
                                    q.append(p)

                            continue

            # A "normal" field or a ptr to a struct that we don't know about
            for i in range(array_sz):
                cur.fields[foff + i * fsize] = fsize

    return root


def extract_structs(codeql_db_path, cache_fname=None):
    if cache_fname is not None:
        res = restore_object(cache_fname)
        if res is not None:
            logger.debug('Restored structs from cache: %s', cache_fname)
            return res

    logger.debug('Extracting structs from CodeQL DB "%s"', codeql_db_path)
    res = run_codeql_query(codeql_db_path, STRUCTS_QUERY)
    structs = {}

    for s, sz, fname, ftype, foff, fsize in res:
        if s != 'struct <unnamed>':
            if s not in structs:
                structs[s] = Struct(int(sz), [])

            foff = int(foff)
            fsize = int(fsize)
            structs[s].fields.append(StructField(fname, ftype, foff, fsize))

    logger.debug('Found %d unique structs', len(structs))

    if cache_fname is not None:
        save_object(structs, cache_fname)

    return structs


def extract_function_pointers(codeql_db_path, cache_fname=None):
    if cache_fname is not None:
        res = restore_object(cache_fname)
        if res is not None:
            logger.debug('Restored function pointers from cache: %s', cache_fname)
            return res

    logger.debug('Extracting global function pointers from CodeQL DB "%s"', codeql_db_path)
    rows = run_codeql_query(codeql_db_path, FUNC_PTRS_QUERY)

    fptr_decls = {}
    fptr_calls = defaultdict(lambda: defaultdict(list))

    for name, typ, decl_loc, *call_loc, exported_func, exported_loc, sig in rows:
        fptr_decls[name] = decl_loc
        fptr_calls[name][tuple(call_loc)].append((exported_func, exported_loc, sig))

    # [(func_ptr_name, call_loc, exported_func, signature)]
    res = []
    seen_locs = set()

    for name, decl_loc in fptr_decls.items():
        calls = fptr_calls[name]

        for call_loc in calls:
            caller, file, *loc_in_file = call_loc
            loc = (file,) + tuple(map(int, loc_in_file))

            if loc not in seen_locs:
                seen_locs.add(loc)

            for exp_func, exp_loc, sig in calls[call_loc]:
                res.append((name, loc, len(seen_locs) - 1, exp_func, sig))

    if cache_fname is not None:
        save_object(res, cache_fname)

    return res

################################################################################

PRIMITIVE_TYPES = {
    'float',
    'double',
    'long double',
    'bool',
    '(unnamed enum)',
    '..(*)(..)',
    'void'
}

INTS = [
    'char',
    'short',
    'int',
    'long',
    'long long',
]

for t in INTS:
    PRIMITIVE_TYPES.add(t)
    PRIMITIVE_TYPES.add(t + ' *')
    PRIMITIVE_TYPES.add('unsigned ' + t)
    PRIMITIVE_TYPES.add('unsigned ' + t + ' *')

STRUCTS_QUERY = '''
import cpp

from Struct s, Field f
where
    f = s.getAField()
select
    s as Struct,
    s.getSize() as StructSize,
    f as Field,
    f.getType().getUnspecifiedType() as Type,
    f.getByteOffset() as Offset,
    f.getType().getSize() as Size
order by Struct, Offset
'''

FUNC_PTRS_QUERY = '''
import cpp

class FancyFunc extends Function {
    predicate isExported() {
        not this.isStatic()
    }

    FancyFunc getACaller() {
        result = this.getACallToThisFunction().getEnclosingFunction()
    }

    FancyFunc getARootExportedFunc() {
        result = this.getACaller*() and result.isExported()
    }

    private string stringifyParam(Parameter p) {
        if p.getUnderlyingType() instanceof Enum
        then result = "int" // NOTE: C++11 enums may have different enum-base
        else result = p.getUnderlyingType().getUnspecifiedType().toString()
    }

    private string sig(Parameter cur, int nLeft) {
        if nLeft = 0
        then result = this.stringifyParam(cur)
        else result = (this.stringifyParam(cur) + ", "
            + this.sig(this.getParameter(this.getNumberOfParameters() - nLeft), nLeft - 1))
    }

    string getSimplifiedSignature() {
        (this.getNumberOfParameters() = 0 and result = "void")
        or
        result = this.sig(this.getParameter(0), this.getNumberOfParameters() - 1)
    }
}

from
    GlobalVariable v,
    Type t,
    FunctionPointerIshType f,
    VariableCall vc,
    VariableAccess va,
    Location l,
    FancyFunc leaf,
    FancyFunc root
where
    not v.isConst()
    and t = v.getType()
    and (f = t or f = t.getUnderlyingType())
    and va = vc.getExpr()
    and v = va.getTarget()
    and l = va.getLocation()
    and leaf = va.getEnclosingFunction()
    and root = leaf.getARootExportedFunc()
select
    v as Variable,
    t as Type,
    v.getLocation().getFile().getRelativePath() + ":" + v.getLocation().getStartLine() as DeclLocation,
    leaf.getQualifiedName() as CallerFunc,
    l.getFile().getRelativePath() as CallFile,
    l.getStartLine() as CallStartRow,
    l.getStartColumn() as CallStartCol,
    l.getEndLine() as CallEndRow,
    l.getEndColumn() as CallEndCol,
    root.getQualifiedName() as ExportedFunc,
    root.getLocation().getFile().getRelativePath() + ":" + root.getLocation().getStartLine() as Location,
    root.getSimplifiedSignature() as Signature
'''

if __name__ == '__main__':
    main(sys.argv)
