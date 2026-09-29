# Structural extraction gold labels

Each fixture repository `backend/tests/fixtures/extraction/<language>/<case>/` has a
manifest `gold/<language>/<case>.yaml` describing what its code **actually does**.
Labels are written from the source (`labelled_from: source`), never copied from
extractor output. A fact the extractor cannot produce yet — a call into another
file, a typed-field call, a declaration hidden by parser error recovery — is
still labelled, so it is measured as a miss instead of disappearing.

`python -m backend.evaluation.extraction_metrics` scores every manifest; see
`docs/evaluation/structural-extraction-baseline.md` for the published results.

## Manifest

```yaml
gold_version: 2                  # bump when labels change meaning
fixture: python/shop_app
language: python
split: holdout                   # or development, see below
labelled_from: source
review: {status: pending_independent_review, reviewer: null}
files:                            # every file in the fixture
  app/legacy.py: {parse_status: PARTIAL, syntax_error_lines: [8], origin: first_party}
  app/_vendor/retry.py: {parse_status: PARSED, origin: vendored}   # first_party | generated | vendored
facts:
  - id: python.shop_app.models.order_add
    kind: entity
    entity_type: METHOD           # CLASS, INTERFACE, FUNCTION or METHOD
    qualified_name: Order.add
    file: app/models.py
    start_line: 26
    end_line: 29
    decorators: []                # as written, without @ or whitespace; Java: annotation names
  - id: python.shop_app.create_order_calls_add
    kind: relationship
    relationship_type: CALLS      # IMPORTS, CALLS, EXTENDS or IMPLEMENTS
    source_key: function:app/controller.py::create_order
    target_key: method:app/models.py::Order.add
    resolution: cross_file        # same_file | cross_file | external | unresolvable | ambiguous
    basis: inferred_type          # internal edges only: how the link should be resolved (below)
    evidence: [{file: app/controller.py, start_line: 10, end_line: 10}]
  - id: python.shop_app.unsupported_dependency_requests
    kind: unsupported             # true, but for a capability the analyzer reports UNSUPPORTED
    capability: dependencies
    entity_type: DEPENDENCY
    name: requests
    file: requirements.txt
```

Keys use the extractor's stable-key format: `<type>:<path>::<qualified name>`,
`file:<path>`, and `external:<name>` for anything the relationship cannot point
at a repository declaration for. CONTAINS facts are not written by hand: the
evaluator derives them from the entities (each declaration's nearest labelled
enclosing declaration, else its file), with the child's range as evidence.
`gold/capabilities.yaml` records the expected capability status per language.

## Resolution

- `same_file` / `cross_file` — the target is a declaration in the repository.
  The evaluator checks this against the two files. Only an edge to that exact
  declaration counts; an unresolved placeholder at the same site is a miss.
- `external` — outside the repository (standard library, third-party).
- `unresolvable` — statically unknowable or with no declaration to link to: calling a
  parameter, a runtime-chosen class, an untyped receiver, an implicit record accessor.
- `ambiguous` — statically ambiguous between known candidates, e.g. a name bound by
  alternative imports in `try`/`except`.

The last three expect an unresolved placeholder named `external:<callee>`.

## Resolution basis (internal edges)

Every `same_file`/`cross_file` edge records the mechanism that justifies the link, judged
from the source at its evidence line. Correctly resolved edges must report this basis and
the certainty it warrants; recall is published per basis.

| Basis | Meaning | Expected certainty |
|---|---|---|
| `lexical_scope` | a bare name bound in an enclosing scope (Python bases and instantiation too) | MEDIUM |
| `enclosing_class` | `self`/`this`/unqualified member, incl. inherited and outer-instance members | MEDIUM |
| `base_class` | `super().x()` / `super.x()` | MEDIUM |
| `constructor` | `new X()`, `super(...)`, `this(...)` | MEDIUM |
| `type_name` | Java `extends`/`implements` and static calls through a type name | MEDIUM |
| `declared_type` | a receiver with a declared type: parameter, field, local, annotation | LOW |
| `constructed_instance` | a method called on a just-constructed object: `Local().run()` | LOW |
| `method_set` | Go: a type implicitly implements an interface its methods cover (name and parameter count) | LOW |
| `import_path` | the file an import statement names | (cross-file; not produced yet) |
| `import_binding` | a name bound by an import (`from .models import User; User()`) | (cross-file) |
| `inferred_type` | a receiver typed only by inference (`user = User(); user.display()`) | (not produced) |

## Origin

`origin` marks generated and vendored files (see `backend/services/extraction/origin.py`).
Their facts are still labelled; results are published per origin so first-party accuracy
is never diluted or flattered by code that is not the repository's own.

## Error categories

Every false positive and false negative is assigned one of: parser query gap,
normalization error (wrong key, name, basis, certainty or origin), receiver-resolution
error, ambiguity, cross-file limitation, evidence-range error, incorrect fixture label —
plus parser error recovery (a declaration Tree-sitter's recovery swallowed) and
unsupported-fact leakage, kept separate because neither is a query gap.

## Splits

- `development` cases may inform analyzer changes; their scores are optimistic.
- `holdout` cases are labelled before, and never used to tune, the analyzer. When a
  holdout case leads to an analyzer change it becomes `development` (noted in the
  manifest) and should be replaced by a new holdout case.

Labels are never weakened to improve metrics. A label may be corrected only when it
contradicts these conventions; the correction is noted on the fact. A mismatch
confirmed to be a fixture or label defect is recorded in `gold/triage.yaml`
(`overrides: {<mismatch id>: {category: fixture/label defect, reason: ...}}`)
rather than by editing the label.

## Conventions

- Qualified names — Python: dotted nesting within the module (`Shape.describe`,
  `OrderService.summary.label`); a redefinition gets `#2`, `#3` in source order.
  Java: package, nested types and signature with parameter types as written
  (`com.acme.shop.OrderService.add(String,int)`); anonymous-class methods are
  `<enclosing method>.<anonymous>.<signature>`; a record's compact constructor takes
  the record components as its signature.
- Line ranges span the whole declaration including decorators or annotations.
- IMPORTS are labelled per statement with `module` as written; the target is what is
  imported: a Python module (its FILE when in the repository) or a Java type (its
  CLASS/INTERFACE when in the repository). Statement extraction and import
  resolution are scored separately.
- CALLS go from the innermost enclosing function/method (the class for field
  initializers and class bodies, the FILE at module level) to the callee:
  declarations in the repository — through `self`/`this`, inheritance, nested
  functions, typed parameters and fields, overloads chosen by argument types —
  are internal targets. Instantiation targets the Python class or the Java
  constructor (the class when none is declared); `super(...)`/`this(...)` target
  the constructor they invoke. Python `super()` is itself a call to `external:super`.
  Everything else is `external:<callee as written, whitespace removed>`, with a
  receiver that is not a plain name or field chain written `<expression>`.
- EXTENDS / IMPLEMENTS target the base type without type arguments.
- Declarations that do not parse are not labelled; `syntax_error_lines` records
  where the error is.

## Scala conventions

- Classes, case classes and enums are `CLASS`; traits are `INTERFACE`; objects and case
  objects are `CLASS` named with a trailing `$` (`kafka.utils.Logging$`), as on the JVM, so a
  companion object never shares a key with its class or trait.
- `def` in a class, object, trait or enum is a `METHOD`; top-level (Scala 3) and local `def`s
  are `FUNCTION`s (`Cart.add(String,Int).expand(Int)`). The signature lists every parameter
  list with types as written, whitespace removed: `inLock(Lock)(=>T)`; type parameters are
  omitted; a parameterless `def size: Int` is just `size`. Auxiliary constructors are
  `this(...)` methods; the primary constructor is the class itself.
- Methods of an anonymous class (`new T { def run() = ... }`) are
  `<enclosing>.<anonymous>.run()`; classes declared inside a `def` use `<local>`.
- IMPORTS: one fact per imported name, all with the statement's evidence. Each selector of
  `a.{B, C => D}` is its own fact (the original name, not the alias); `X => _` imports nothing;
  `_` and `*` wildcards are written `a.*`. A repository type or object is the target; when a
  trait or class and its companion share the name, the type is the target. A wildcard over a
  repository package is `unresolvable` (a package has no declaration).
- EXTENDS / IMPLEMENTS follow the syntax: a class's or object's first parent is `EXTENDS`, each
  further parent (`with`, or Scala 3 `,`) is `IMPLEMENTS`; a trait's parents are all `EXTENDS`.
- CALLS are explicit applications: `f(x)`, `a.f(x)`, `a.f { ... }`, `new C(...)`,
  `new T { ... }`, `C(...)` (an `apply`), `this(...)` in an auxiliary constructor,
  `extends Base(x)` (from the class to Base's constructor) and alphanumeric infix calls
  (`xs foreach f`, target `xs.foreach`). Symbolic operators (`a + b`, `x :: xs`, `buf += x`),
  parameterless selections (`xs.size`), string interpolators and functions passed as values
  (`xs.foreach(println)`) are not calls.
- A curried application `f(a)(b)` is one call whose evidence is its first application `f(a)`.
- `Name(...)` targets the companion object's `apply` (basis `type_name`) when the object
  declares one, else the class (the synthetic or universal apply; basis `constructor`).
  `new T { ... }` targets T, including a trait. `Name.m(...)` through an object has basis
  `type_name`.

## Go conventions

- Qualified names use the package name: `shapes.Circle`, `shapes.NewCircle`; methods are
  `<package>.<receiver type>.<method>` whatever the receiver's pointer-ness; interface methods
  are METHODs of the interface. Go has no overloading, so there are no signatures; a name
  declared twice (e.g. `init`) gets `#2`.
- Named types are `CLASS` (struct, slice, func or basic underlying types alike); interfaces are
  `INTERFACE`; type aliases (`type A = B`) are not declarations. A lone `type T ...` spans the
  `type` keyword; a spec inside `type ( ... )` spans itself. A method's parent is its receiver
  type when that is declared in the same file, else the file.
- IMPORTS: one fact per import spec, `module` the path as written. A package inside the
  repository has no declaration to link to, so its import is `unresolvable`.
- EXTENDS: each embedded field of a struct or interface (the embedded type without pointer or
  type arguments).
- IMPLEMENTS is implicit: labelled when a repository type's method set (its own and promoted
  methods, value or pointer receivers) covers every method of a repository interface, with basis
  `method_set` and the implementing type's name line as evidence. Implementations of interfaces
  outside the repository are not labelled.
- CALLS: `f(x)`, `pkg.F(x)`, `x.M(x)`, including builtins (`len`, `make`) and calls inside
  closures, which belong to the enclosing function. Conversions (`Meters(x)`, `time.Duration(n)`,
  `string(b)`) and composite literals are not calls; neither are method values
  (`s.handler.Handle` passed as an argument). The receiver of a method is its `this`
  (`enclosing_class`); calls through a package qualifier into the repository are
  `import_binding`; a function defined in several build-tagged files is `ambiguous`.

## JavaScript conventions

- Qualified names are module-relative and dotted by nesting, as in Python: `Cart`, `Cart.add`,
  `Cart.#items`-style private names as written, `checkout`, `handler.format` for a function
  nested in `handler`. There are no signatures; getters and setters are named like the property.
- `CLASS`: class declarations and classes bound to a name (`const A = class {}`); an anonymous
  `export default class` is `default`. `METHOD`: methods (static, private, async, accessors,
  `constructor`) and class fields whose value is a function. `FUNCTION`: function declarations,
  functions and arrows bound to a name, `exports.name = function` / `module.exports.name = ...`
  (named `name`), and the functions of an object literal bound to a name (`api.get`).
  Anonymous callbacks are not declarations: their calls belong to the enclosing declaration.
- Spans exclude `export` and JSDoc; a lone `const f = ...` spans the whole statement.
- IMPORTS: one fact per `import`, `export ... from`, `require('m')` and `import('m')`, with
  `module` the specifier as written. A relative specifier targets the repository FILE it resolves
  to (Node resolution, e.g. `./pricing` is `pricing.js`); packages are external.
- EXTENDS: `class A extends B` when B is a name or member chain (mixin calls are not labelled).
- CALLS: `f()`, `a.b()`, `new C()` (C's `constructor` method, else the class), `super(...)`
  (the base class's constructor, else the base class), including builtins. `this` is the class in
  methods and in arrows inside them, not in nested `function`s. JavaScript has no declared types:
  JSDoc `@param {T} x` and `@type {T}` count as declared (`declared_type`); anything else,
  including `const x = new C()`, is inferred. `obj.f()` on an object literal bound in scope is
  `lexical_scope`; a static call through a class name is `type_name`; a name bound to one of
  several functions at run time is `ambiguous`.

## Ruby conventions

- Qualified names are dotted by nesting: `Shop.Cart`, `Shop.Cart.total`; singleton methods
  (`def self.x`, or any `def` inside `class << self`) are `Shop.Cart.self.x`. Classes are
  `CLASS`; modules are `INTERFACE` (Ruby's mixins, also used as namespaces); `def` in a class or
  module is a `METHOD`, at the top level a `FUNCTION` (a redefinition on another branch gets `#2`).
  Methods generated by metaprogramming (`attr_reader`, `define_method`, associations) are not
  declarations.
- IMPORTS: `require`, `require_relative` and `load` with a literal path, `module` as written.
  `require_relative` targets the repository file it names (`.rb` added); gems are external.
- EXTENDS: `class A < B`. IMPLEMENTS: each module in `include`, `prepend` or `extend`.
- CALLS: every `recv.m` and `m(args)` (Ruby has no public fields), and a bare name that is not a
  local variable, parameter or block parameter, which Ruby itself treats as a call on `self`.
  Not calls: class-body declarations (`include`, `extend`, `prepend`, `attr_*`, `private`,
  `protected`, `public`, `module_function`), `require*`, keywords (`yield`, `retry`,
  `defined?`), and symbols passed as methods (`&:sku`). Other class-body calls such as
  `has_many` are calls from the class.
- Resolution: calls on `self`, bare calls and mixed-in or inherited methods are
  `enclosing_class`; `Foo.new` (or `new` in a class method) targets `Foo#initialize`, else the
  class (`constructor`); `Foo.bar` targets the singleton method (`type_name`); `super` targets the
  same method of the superclass (`base_class`); YARD `@param name [Type]` counts as declared
  (`declared_type`). A call on a parameter or instance variable of unknown type is
  `unresolvable`; on an Array or other value created by a literal it is `external`.
