"""Language analyzers: Python, Java, Scala, Go, JavaScript and Ruby over Tree-sitter, plus the file-level fallback.

Resolution is deliberately local to one file. A reference is linked to an
entity only when the file's own declarations determine it: lexical scopes and
shadowing (Python, Scala), class members including same-file base and outer classes,
overloads by argument count and typed receivers (Java, Scala). Anything that is
ambiguous or declared elsewhere becomes an unresolved ``EXTERNAL_SYMBOL``;
cross-file linking is not attempted, which is why calls and inheritance are
reported as PARTIAL.

A declaration whose own header does not parse (for example ``def f(:``) is
skipped together with its body: its name and extent are not trustworthy.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .builder import FileFactsBuilder, Span
from .contracts import (
    AnalyzerCapabilities,
    CancellationToken,
    Capability,
    CapabilityLevel,
    Certainty,
    EntityType,
    FileExtractionError,
    RelationshipType,
)
from .tree_sitter_adapter import (
    CANCELLATION_CHECK_INTERVAL,
    TreeSitterParserAdapter,
    span_of,
    syntax_errors,
    text_of,
    walk,
)

S, P, U = CapabilityLevel.SUPPORTED, CapabilityLevel.PARTIAL, CapabilityLevel.UNSUPPORTED

# How a resolved reference was linked, and how far that link can be trusted. Scope-based links
# follow the language's own name lookup (MEDIUM). Links through a receiver depend on a declared
# or constructed type that the analyzer does not verify (LOW), so consumers can filter them.
# Unresolved placeholders are LOW with metadata resolution "unresolved".
RESOLUTION_BASIS = {
    "lexical": ("lexical_scope", Certainty.MEDIUM),
    "lexical_class": ("lexical_scope", Certainty.MEDIUM),
    "self": ("enclosing_class", Certainty.MEDIUM),
    "unqualified": ("enclosing_class", Certainty.MEDIUM),
    "this": ("enclosing_class", Certainty.MEDIUM),
    "super": ("base_class", Certainty.MEDIUM),
    "type": ("type_name", Certainty.MEDIUM),
    "new": ("constructor", Certainty.MEDIUM),
    "this_constructor": ("constructor", Certainty.MEDIUM),
    "super_constructor": ("constructor", Certainty.MEDIUM),
    "receiver": ("declared_type", Certainty.LOW),
    "instance": ("constructed_instance", Certainty.LOW),
    # Go: a type implements an interface implicitly when its method set covers the interface's.
    # Matched by method name and parameter count, not full signatures.
    "method_set": ("method_set", Certainty.LOW),
}
_MAX_NAME = 200
_WHITESPACE = re.compile(r"\s+")
EXPRESSION = "<expression>"


def _name(text: str) -> str:
    return _WHITESPACE.sub("", text)[:_MAX_NAME]


def _header_broken(node: Any, *fields: str) -> bool:
    for name in ("name", *fields):
        child = node.child_by_field_name(name)
        if name == "name" and child is None:
            return True
        if child is not None and (child.has_error or child.is_missing):
            return True
    return False


class FileLevelAnalyzer:
    """Fallback for languages without an analyzer: the file itself and nothing inferred."""

    language = "*"
    extractor = "stacksniffer-file"
    extractor_version = "file/1"
    capabilities = AnalyzerCapabilities.none()
    available = True

    def analyze(self, source: bytes, builder: FileFactsBuilder, token: CancellationToken) -> list[FileExtractionError]:
        return []


@dataclass(frozen=True)
class _Scope:
    key: str                 # entity that owns declarations made here
    kind: str                # "file", "class" or "callable"
    qualified_name: str | None
    class_key: str | None    # class whose members `self`/`this` refer to
    callable_key: str | None  # innermost callable: the source of calls made here
    namespace: str           # where names declared here are bound (Python lexical scoping)
    body_id: int | None      # node id of the block whose direct children are unconditional


@dataclass(frozen=True)
class _Declaration:
    key: str
    line: int
    unconditional: bool


@dataclass(frozen=True)
class _Reference:
    relationship_type: RelationshipType
    source_key: str
    span: Span
    kind: str        # external symbol kind when unresolved
    display: str     # external symbol name when unresolved
    lookup: str
    name: str = ""
    scope: _Scope | None = None
    arguments: int | None = None
    receiver: str | None = None
    metadata: dict = field(default_factory=dict)


class _FileState:
    def __init__(self) -> None:
        self.names: dict[str, dict[str, list[_Declaration]]] = defaultdict(lambda: defaultdict(list))
        self.bound: dict[str, set[str]] = defaultdict(set)
        self.parent_namespace: dict[str, str | None] = {}
        self.types: dict[str, list[str]] = defaultdict(list)
        self.classes: set[str] = set()
        self.members: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        self.bases: dict[str, list[str]] = defaultdict(list)
        self.superclass: dict[str, str] = {}
        self.outer: dict[str, str] = {}
        self.constructors: dict[str, list[str]] = defaultdict(list)
        self.arity: dict[str, tuple[float, float]] = {}
        self.variables: dict[str, dict[str, str]] = defaultdict(dict)
        self.references: list[_Reference] = []
        self.pending_decorators: dict[int, list[str]] = {}
        # RESOLUTION_BASIS key decided during lookup, keyed by id() of the reference.
        self.basis_override: dict[int, str] = {}
        self.pending_bases: list[tuple[str, str, _Scope, int]] = []
        self.package: str | None = None
        self.objects: dict[str, list[str]] = defaultdict(list)  # Scala singleton objects by name
        self.traits: set[str] = set()
        self.handled: set[int] = set()  # node ids a language walked out of order (JavaScript)
        self.js_objects: dict[tuple[str, str], str] = {}  # (namespace, name) -> object-literal owner


class TreeSitterAnalyzer:
    """Shared walk/resolve machinery. Subclasses implement ``visit`` and ``candidates``."""

    language: str
    extractor: str
    analyzer_version: str
    capabilities: AnalyzerCapabilities

    def __init__(self, adapter: TreeSitterParserAdapter):
        self.adapter = adapter

    @property
    def available(self) -> bool:
        return self.adapter.available

    @property
    def extractor_version(self) -> str:
        return f"{self.analyzer_version}+{self.adapter.grammar_version}"

    def analyze(self, source: bytes, builder: FileFactsBuilder, token: CancellationToken) -> list[FileExtractionError]:
        token.raise_if_cancelled()
        tree = self.adapter.parse(source)
        token.raise_if_cancelled()
        errors = syntax_errors(tree.root_node, builder.path, token)

        state = _FileState()
        root = tree.root_node
        root_scope = _Scope(builder.file_key, "file", None, None, None, builder.file_key, root.id)
        state.parent_namespace[builder.file_key] = None
        stack: list[tuple[Any, _Scope]] = [(root, root_scope)]
        visited = 0
        while stack:
            node, scope = stack.pop()
            visited += 1
            if visited % CANCELLATION_CHECK_INTERVAL == 0:
                token.raise_if_cancelled()
            child_scope = self.visit(node, scope, state, builder)
            if child_scope is not None:
                stack.extend((child, child_scope) for child in reversed(node.children))

        token.raise_if_cancelled()
        self.prepare(state)
        for ref in state.references:
            candidates = self.candidates(ref, state)
            metadata = dict(ref.metadata)
            if len(candidates) == 1:
                # Lookup decided how the name resolved when one reference form allows several
                # (e.g. a static call through a type name looks like a call on a variable).
                basis, certainty = RESOLUTION_BASIS[state.basis_override.get(id(ref), ref.lookup)]
                target = candidates[0]
                metadata["resolution"] = "same_file"
                metadata["resolution_basis"] = basis
            else:
                target = builder.external(ref.kind, ref.display, ref.span)
                certainty = Certainty.LOW
                metadata["resolution"] = "unresolved"
                if candidates:
                    metadata["ambiguous_candidates"] = len(candidates)
            builder.relationship(ref.relationship_type, ref.source_key, target, ref.span,
                                 certainty=certainty, metadata=metadata)
        self.finish(state, builder)
        return errors

    def visit(self, node: Any, scope: _Scope, state: _FileState, builder: FileFactsBuilder) -> _Scope | None:
        raise NotImplementedError

    def prepare(self, state: _FileState) -> None:
        """Hook run after the walk, before references are resolved."""

    def finish(self, state: _FileState, builder: FileFactsBuilder) -> None:
        """Hook run after references are resolved, for facts no reference site states."""

    def candidates(self, ref: _Reference, state: _FileState) -> list[str]:
        raise NotImplementedError

    # -- shared lookups ---------------------------------------------------------------------

    def _members(self, state: _FileState, class_key: str | None, name: str, *, arguments: int | None = None,
                 outer: bool = False, bases_only: bool = False) -> list[str]:
        """Members named ``name`` of a class, else of its same-file bases, else (optionally) outer classes."""
        seen: set[str] = set()
        current = class_key
        while current:
            found = self._inherited(state, current, name, arguments, seen, skip_self=bases_only)
            if found or not outer:
                return found
            current = state.outer.get(current)
        return []

    def _inherited(self, state, class_key, name, arguments, seen, *, skip_self=False) -> list[str]:
        if class_key in seen:
            return []
        seen.add(class_key)
        if not skip_self:
            found = self._by_arity(state, state.members[class_key].get(name, []), arguments)
            if found:
                return found
        for base in state.bases.get(class_key, []):
            found = self._inherited(state, base, name, arguments, seen)
            if found:
                return found
        return []

    @staticmethod
    def _by_arity(state: _FileState, keys: list[str], arguments: int | None) -> list[str]:
        if arguments is None or len(keys) <= 1:
            return keys
        matching = [key for key in keys if key not in state.arity
                    or state.arity[key][0] <= arguments <= state.arity[key][1]]
        return matching

    @staticmethod
    def _qualify(scope: _Scope, name: str, prefix: str | None = None) -> str:
        if scope.qualified_name:
            return f"{scope.qualified_name}.{name}"
        return f"{prefix}.{name}" if prefix else name


# --- Python ----------------------------------------------------------------------------------


def _python_receiver_is_simple(node: Any) -> bool:
    while node.type == "attribute":
        node = node.child_by_field_name("object")
    return node.type == "identifier"


def _python_bound_names(target: Any) -> list[str]:
    if target is None:
        return []
    if target.type == "identifier":
        return [text_of(target)]
    if target.type in ("pattern_list", "tuple_pattern", "list_pattern", "list_splat_pattern"):
        return [name for child in target.named_children for name in _python_bound_names(child)]
    return []


def _python_parameter_names(parameters: Any) -> list[str]:
    names = []
    for parameter in parameters.named_children if parameters is not None else []:
        if parameter.type == "identifier":
            names.append(text_of(parameter))
        elif parameter.type in ("list_splat_pattern", "dictionary_splat_pattern"):
            names.extend(text_of(c) for c in parameter.named_children if c.type == "identifier")
        else:
            name = parameter.child_by_field_name("name")
            if name is None:
                name = next((c for c in parameter.named_children if c.type == "identifier"), None)
            if name is not None:
                names.append(text_of(name))
    return names


class PythonAnalyzer(TreeSitterAnalyzer):
    language = "python"
    extractor = "tree-sitter-python"
    analyzer_version = "stacksniffer-python/2"
    capabilities = AnalyzerCapabilities({
        Capability.DECLARATIONS: S,
        Capability.IMPORTS: S,
        Capability.CALLS: P,
        Capability.INHERITANCE: P,
        Capability.INTERFACES: U,  # Protocols/ABCs are not inferred
        Capability.DEPENDENCIES: U,
        Capability.DECORATORS: S,
    })

    def __init__(self, adapter: TreeSitterParserAdapter | None = None):
        super().__init__(adapter or TreeSitterParserAdapter("python", "tree_sitter_python", "tree-sitter-python"))

    def visit(self, node, scope, state, builder):
        kind = node.type
        if kind == "decorated_definition":
            definition = node.child_by_field_name("definition")
            if definition is not None:
                state.pending_decorators[definition.id] = [
                    _name(text_of(child).lstrip("@")) for child in node.children if child.type == "decorator"
                ]
            return scope
        if kind == "class_definition":
            return self._class(node, scope, state, builder)
        if kind == "function_definition":
            return self._function(node, scope, state, builder)
        if kind == "import_statement":
            for name_node in node.children_by_field_name("name"):
                module_node = name_node.child_by_field_name("name") if name_node.type == "aliased_import" else name_node
                alias = name_node.child_by_field_name("alias")
                self._import(builder, _name(text_of(module_node)), node, name_node,
                             {"alias": text_of(alias)} if alias is not None else {})
            return None
        if kind == "import_from_statement":
            module_node = node.child_by_field_name("module_name")
            names = [_name(text_of(n.child_by_field_name("name") if n.type == "aliased_import" else n))
                     for n in node.children_by_field_name("name")]
            if any(child.type == "wildcard_import" for child in node.children):
                names.append("*")
            self._import(builder, _name(text_of(module_node)), node, module_node, {"names": names})
            return None
        if scope.kind == "callable":
            if kind in ("assignment", "augmented_assignment"):
                state.bound[scope.namespace].update(_python_bound_names(node.child_by_field_name("left")))
            elif kind == "for_statement":
                state.bound[scope.namespace].update(_python_bound_names(node.child_by_field_name("left")))
        if kind == "call":
            self._call(node, scope, state)
        return scope

    def _import(self, builder, module, statement, name_node, metadata):
        if not module:
            return
        target = builder.external("module", module, span_of(name_node))
        builder.relationship(RelationshipType.IMPORTS, builder.file_key, target, span_of(statement),
                             certainty=Certainty.EXACT,
                             metadata={**metadata, "module": module, "resolution": "unresolved"})

    @staticmethod
    def _declaration_span(node, state) -> tuple[Span, list[str] | None]:
        decorators = state.pending_decorators.pop(node.id, None)
        outer = node.parent if decorators is not None and node.parent is not None else node
        return span_of(outer), decorators

    @staticmethod
    def _declare(state, scope, name, key, node):
        container = node.parent.parent if node.parent is not None and node.parent.type == "decorated_definition" else node.parent
        unconditional = container is not None and container.id == scope.body_id
        state.names[scope.namespace][name].append(_Declaration(key, span_of(node).start_line, unconditional))

    def _class(self, node, scope, state, builder):
        if _header_broken(node, "superclasses"):
            return None
        name = text_of(node.child_by_field_name("name")).strip()
        qualified = self._qualify(scope, name)
        span, decorators = self._declaration_span(node, state)
        metadata = {"decorators": decorators} if decorators else {}
        key = builder.entity(EntityType.CLASS, name, span, parent_key=scope.key,
                             qualified_name=qualified, metadata=metadata)
        state.classes.add(key)
        self._declare(state, scope, name, key, node)
        superclasses = node.child_by_field_name("superclasses")
        for base in superclasses.named_children if superclasses is not None else []:
            if base.type == "subscript":  # Generic[T] -> Generic
                base = base.child_by_field_name("value") or base
            if base.type in ("identifier", "attribute"):
                base_name = _name(text_of(base))
                state.references.append(_Reference(
                    RelationshipType.EXTENDS, key, span_of(base), "type", base_name,
                    lookup="lexical_class" if base.type == "identifier" else "none", name=base_name, scope=scope))
                if base.type == "identifier":
                    state.pending_bases.append((key, base_name, scope, span_of(base).start_line))
        state.parent_namespace[key] = scope.namespace
        body = node.child_by_field_name("body")
        return _Scope(key, "class", qualified, key, None, key, body.id if body is not None else None)

    def _function(self, node, scope, state, builder):
        if _header_broken(node, "parameters"):
            return None
        name = text_of(node.child_by_field_name("name")).strip()
        is_method = scope.kind == "class"
        qualified = self._qualify(scope, name)
        span, decorators = self._declaration_span(node, state)
        parameters = node.child_by_field_name("parameters")
        metadata: dict[str, Any] = {
            "async": any(child.type == "async" for child in node.children),
            "parameters": _WHITESPACE.sub(" ", text_of(parameters))[:500],
        }
        if decorators:
            metadata["decorators"] = decorators
        key = builder.entity(EntityType.METHOD if is_method else EntityType.FUNCTION, name, span,
                             parent_key=scope.key, qualified_name=qualified, metadata=metadata)
        if is_method:
            state.members[scope.class_key][name].append(key)
        self._declare(state, scope, name, key, node)
        # Function bodies see enclosing function and module names, never class-body names.
        enclosing = state.parent_namespace[scope.namespace] if scope.kind == "class" else scope.namespace
        state.parent_namespace[key] = enclosing
        state.bound[key].update(_python_parameter_names(parameters))
        body = node.child_by_field_name("body")
        return _Scope(key, "callable", qualified, scope.class_key, key, key, body.id if body is not None else None)

    def _call(self, node, scope, state):
        function = node.child_by_field_name("function")
        source = scope.callable_key or scope.key
        if function is None:
            return
        span = span_of(node)
        if function.type == "identifier":
            name = _name(text_of(function))
            state.references.append(_Reference(RelationshipType.CALLS, source, span, "call", name,
                                               lookup="lexical", name=name, scope=scope))
            return
        if function.type != "attribute":
            return
        receiver = function.child_by_field_name("object")
        attribute = _name(text_of(function.child_by_field_name("attribute")))
        if receiver is None or not attribute:
            return
        simple = _python_receiver_is_simple(receiver)
        display = _name(text_of(function)) if simple else f"{EXPRESSION}.{attribute}"
        if receiver.type == "identifier" and text_of(receiver) in ("self", "cls"):
            lookup, receiver_name = "self", None
        elif receiver.type == "call" and text_of(receiver.child_by_field_name("function")) == "super":
            lookup, receiver_name = "super", None
        elif receiver.type == "call" and (callee := receiver.child_by_field_name("function")) is not None \
                and callee.type == "identifier":
            lookup, receiver_name = "instance", text_of(callee)  # Local().run()
        else:
            lookup, receiver_name = "none", None
        state.references.append(_Reference(RelationshipType.CALLS, source, span, "call", display, lookup=lookup,
                                           name=attribute, scope=scope, receiver=receiver_name))

    def prepare(self, state) -> None:
        """Record same-file base classes so inherited `self.x()` and `super().x()` resolve."""
        for class_key, name, scope, line in state.pending_bases:
            found = [key for key in self._lexical(state, scope, name, line) if key in state.classes]
            if len(found) == 1:
                state.bases[class_key].append(found[0])

    def candidates(self, ref, state):
        scope = ref.scope
        if ref.lookup in ("lexical", "lexical_class"):
            found = self._lexical(state, scope, ref.name, ref.span.start_line)
            if ref.lookup == "lexical_class":
                found = [key for key in found if key in state.classes]
            return found
        if ref.lookup == "self":
            return self._members(state, scope.class_key, ref.name)
        if ref.lookup == "super":
            return self._members(state, scope.class_key, ref.name, bases_only=True)
        if ref.lookup == "instance":
            classes = [key for key in self._lexical(state, scope, ref.receiver, ref.span.start_line)
                       if key in state.classes]
            return self._members(state, classes[0], ref.name) if len(classes) == 1 else []
        return []

    @staticmethod
    def _lexical(state, scope, name, line) -> list[str]:
        """Resolve a bare name through enclosing function and module namespaces."""
        namespace = scope.namespace
        at_module_level = scope.callable_key is None
        while namespace is not None:
            declarations = state.names[namespace].get(name)
            if declarations:
                if len(declarations) == 1:
                    return [declarations[0].key]
                if all(d.unconditional for d in declarations):
                    # Code inside functions runs after the namespace is complete: the last binding wins.
                    # At module level only definitions above the call are bound yet.
                    usable = declarations if not at_module_level else [d for d in declarations if d.line <= line]
                    return [usable[-1].key] if usable else []
                return [d.key for d in declarations]  # conditional redefinitions stay ambiguous
            if name in state.bound[namespace]:
                return []  # shadowed by a parameter or local assignment
            namespace = state.parent_namespace.get(namespace)
        return []


# --- Java ------------------------------------------------------------------------------------

_JAVA_TYPE_DECLARATIONS = {
    "class_declaration": (EntityType.CLASS, "class"),
    "enum_declaration": (EntityType.CLASS, "enum"),
    "record_declaration": (EntityType.CLASS, "record"),
    "interface_declaration": (EntityType.INTERFACE, "interface"),
    "annotation_type_declaration": (EntityType.INTERFACE, "annotation"),
}
_JAVA_COMMENTS = {"line_comment", "block_comment"}


def _java_receiver_is_simple(node: Any) -> bool:
    while node.type == "field_access":
        node = node.child_by_field_name("object")
    return node.type in ("identifier", "this", "super", "scoped_identifier")


def _java_type_name(node: Any) -> str | None:
    """The type's name without type arguments; None for primitives and arrays."""
    if node is None:
        return None
    if node.type == "generic_type":
        node = next((c for c in node.named_children if c.type != "type_arguments"), None)
    if node is None or node.type not in ("type_identifier", "scoped_type_identifier"):
        return None
    return _name(text_of(node))


def _java_argument_count(node: Any) -> int | None:
    arguments = node.child_by_field_name("arguments")
    if arguments is None:
        return None
    return sum(1 for child in arguments.named_children if child.type not in _JAVA_COMMENTS)


class JavaAnalyzer(TreeSitterAnalyzer):
    language = "java"
    extractor = "tree-sitter-java"
    analyzer_version = "stacksniffer-java/2"
    capabilities = AnalyzerCapabilities({
        Capability.DECLARATIONS: S,
        Capability.IMPORTS: S,
        Capability.CALLS: P,
        Capability.INHERITANCE: P,
        Capability.INTERFACES: P,
        Capability.DEPENDENCIES: U,
        Capability.DECORATORS: S,  # annotations
    })

    def __init__(self, adapter: TreeSitterParserAdapter | None = None):
        super().__init__(adapter or TreeSitterParserAdapter("java", "tree_sitter_java", "tree-sitter-java"))

    def visit(self, node, scope, state, builder):
        kind = node.type
        if kind == "package_declaration":
            name = next((c for c in node.named_children if c.type in ("scoped_identifier", "identifier")), None)
            state.package = _name(text_of(name)) or None
            return None
        if kind == "import_declaration":
            self._import(node, builder)
            return None
        if kind in _JAVA_TYPE_DECLARATIONS:
            return self._type(node, scope, state, builder)
        if kind in ("method_declaration", "constructor_declaration", "compact_constructor_declaration"):
            return self._method(node, scope, state, builder)
        if kind in ("field_declaration", "local_variable_declaration"):
            self._variables(node, scope, state)
        elif kind == "method_invocation":
            self._invocation(node, scope, state)
        elif kind == "object_creation_expression":
            self._creation(node, scope, state)
        elif kind == "explicit_constructor_invocation":
            self._constructor_invocation(node, scope, state)
        return scope

    def _import(self, node, builder):
        name_node = next((c for c in node.named_children if c.type in ("scoped_identifier", "identifier")), None)
        if name_node is None:
            return
        wildcard = any(child.type == "asterisk" for child in node.children)
        name = _name(text_of(name_node)) + (".*" if wildcard else "")
        target = builder.external("import", name, span_of(name_node))
        builder.relationship(RelationshipType.IMPORTS, builder.file_key, target, span_of(node),
                             certainty=Certainty.EXACT, metadata={
                                 "module": name,
                                 "static": any(child.type == "static" for child in node.children),
                                 "wildcard": wildcard,
                                 "resolution": "unresolved",
                             })

    @staticmethod
    def _annotations(node) -> list[str]:
        modifiers = next((c for c in node.children if c.type == "modifiers"), None)
        if modifiers is None:
            return []
        return [_name(text_of(a.child_by_field_name("name")))
                for a in modifiers.named_children if a.type in ("marker_annotation", "annotation")]

    def _type(self, node, scope, state, builder):
        if _header_broken(node):
            return None
        name = text_of(node.child_by_field_name("name")).strip()
        entity_type, declaration_kind = _JAVA_TYPE_DECLARATIONS[node.type]
        if scope.kind == "callable":
            qualified = f"{scope.qualified_name}.<local>.{name}"
        else:
            qualified = self._qualify(scope, name, state.package)
        metadata: dict[str, Any] = {"kind": declaration_kind}
        if annotations := self._annotations(node):
            metadata["annotations"] = annotations
        key = builder.entity(entity_type, name, span_of(node), parent_key=scope.key,
                             qualified_name=qualified, metadata=metadata)
        state.types[name].append(key)
        state.classes.add(key)
        if scope.class_key:
            state.outer[key] = scope.class_key

        superclass = node.child_by_field_name("superclass")
        if superclass is not None:
            for type_node in superclass.named_children:
                self._type_reference(RelationshipType.EXTENDS, key, type_node, scope, state, superclass=True)
        for container, relationship in (
            (node.child_by_field_name("interfaces"), RelationshipType.IMPLEMENTS),
            (next((c for c in node.children if c.type == "extends_interfaces"), None), RelationshipType.EXTENDS),
        ):
            type_list = next((c for c in container.named_children if c.type == "type_list"), None) if container else None
            for type_node in type_list.named_children if type_list is not None else []:
                self._type_reference(relationship, key, type_node, scope, state)
        return _Scope(key, "class", qualified, key, None, key, None)

    @staticmethod
    def _type_reference(relationship, source_key, type_node, scope, state, *, superclass=False):
        if type_node.type == "generic_type":
            type_node = next((c for c in type_node.named_children if c.type != "type_arguments"), type_node)
        full_name = _name(text_of(type_node))
        # Only simple names can match a same-file declaration; qualified names stay unresolved.
        lookup = "type" if type_node.type == "type_identifier" else "none"
        state.references.append(_Reference(relationship, source_key, span_of(type_node), "type", full_name,
                                           lookup=lookup, name=full_name, scope=scope))
        if lookup == "type":
            state.bases[source_key].append(full_name)  # names until prepare() resolves them
            if superclass:
                state.superclass[source_key] = full_name

    def _method(self, node, scope, state, builder):
        if _header_broken(node, "parameters"):
            return None
        name = text_of(node.child_by_field_name("name")).strip()
        constructor = node.type in ("constructor_declaration", "compact_constructor_declaration")
        parameters = node.child_by_field_name("parameters")
        if node.type == "compact_constructor_declaration":
            # A record's compact canonical constructor takes the record components.
            record = node.parent.parent if node.parent is not None else None
            parameters = record.child_by_field_name("parameters") if record is not None else None
        types, required, variadic = [], 0, False
        for parameter in parameters.named_children if parameters is not None else []:
            if parameter.type in ("formal_parameter", "spread_parameter"):
                type_node = parameter.child_by_field_name("type") or next(
                    (c for c in parameter.named_children if c.type != "variable_declarator"), None)
                variadic = variadic or parameter.type == "spread_parameter"
                required += parameter.type != "spread_parameter"
                types.append(_name(text_of(type_node)) + ("..." if parameter.type == "spread_parameter" else ""))
        signature = f"{name}({','.join(types)})"

        body_parent = node.parent.parent if node.parent is not None else None
        anonymous = body_parent is not None and body_parent.type == "object_creation_expression"
        if anonymous:
            qualified = f"{scope.qualified_name}.<anonymous>.{signature}"
            class_key = f"anonymous@{node.parent.id}"
            state.outer.setdefault(class_key, scope.class_key)
        else:
            qualified = self._qualify(scope, signature)
            class_key = scope.class_key if scope.kind == "class" else None
        metadata: dict[str, Any] = {"signature": signature, "constructor": constructor}
        if node.type == "compact_constructor_declaration":
            metadata["compact"] = True
        if annotations := self._annotations(node):
            metadata["annotations"] = annotations
        key = builder.entity(EntityType.METHOD, name, span_of(node), parent_key=scope.key,
                             qualified_name=qualified, metadata=metadata)
        state.arity[key] = (required, math.inf if variadic else required)
        if class_key:
            if constructor:
                state.constructors[class_key].append(key)
            else:
                state.members[class_key][name].append(key)
        for parameter in parameters.named_children if parameters is not None else []:
            type_name = _java_type_name(parameter.child_by_field_name("type"))
            variable = parameter.child_by_field_name("name")
            if type_name and variable is not None:
                state.variables[key][text_of(variable)] = type_name
        return _Scope(key, "callable", qualified, class_key, key, key, None)

    @staticmethod
    def _variables(node, scope, state):
        type_name = _java_type_name(node.child_by_field_name("type"))
        owner = scope.callable_key or scope.class_key
        if not type_name or owner is None:
            return
        for declarator in node.children_by_field_name("declarator"):
            variable = declarator.child_by_field_name("name")
            if variable is not None:
                state.variables[owner][text_of(variable)] = type_name

    def _invocation(self, node, scope, state):
        name = _name(text_of(node.child_by_field_name("name")))
        if not name:
            return
        receiver = node.child_by_field_name("object")
        source = scope.callable_key or scope.key
        arguments = _java_argument_count(node)
        span = span_of(node)
        if receiver is None:
            ref = _Reference(RelationshipType.CALLS, source, span, "call", name, lookup="unqualified",
                             name=name, scope=scope, arguments=arguments)
        elif receiver.type in ("this", "super"):
            ref = _Reference(RelationshipType.CALLS, source, span, "call", f"{receiver.type}.{name}",
                             lookup=receiver.type, name=name, scope=scope, arguments=arguments)
        else:
            simple = _java_receiver_is_simple(receiver)
            display = f"{_name(text_of(receiver)) if simple else EXPRESSION}.{name}"
            receiver_name = None
            if receiver.type == "identifier":
                receiver_name = text_of(receiver)
            elif receiver.type == "field_access" and (obj := receiver.child_by_field_name("object")) is not None \
                    and obj.type == "this":
                receiver_name = "this." + text_of(receiver.child_by_field_name("field"))
            ref = _Reference(RelationshipType.CALLS, source, span, "call", display,
                             lookup="receiver" if receiver_name else "none", name=name, scope=scope,
                             arguments=arguments, receiver=receiver_name)
        state.references.append(ref)

    def _creation(self, node, scope, state):
        type_name = _java_type_name(node.child_by_field_name("type"))
        if not type_name:
            return
        source = scope.callable_key or scope.key
        state.references.append(_Reference(
            RelationshipType.CALLS, source, span_of(node), "call", type_name, lookup="new", name=type_name,
            scope=scope, arguments=_java_argument_count(node)))

    def _constructor_invocation(self, node, scope, state):
        constructor = node.child_by_field_name("constructor")
        if constructor is None or constructor.type not in ("this", "super") or scope.callable_key is None:
            return
        state.references.append(_Reference(
            RelationshipType.CALLS, scope.callable_key, span_of(node), "call", constructor.type,
            lookup=f"{constructor.type}_constructor", name=constructor.type, scope=scope,
            arguments=_java_argument_count(node)))

    # -- resolution -------------------------------------------------------------------------

    def candidates(self, ref, state):
        scope = ref.scope
        if ref.lookup == "type":
            return self._type_keys(state, ref.name)
        if ref.lookup == "unqualified":
            return self._members(state, scope.class_key, ref.name, arguments=ref.arguments, outer=True)
        if ref.lookup == "this":
            return self._members(state, scope.class_key, ref.name, arguments=ref.arguments)
        if ref.lookup == "super":
            return self._members(state, scope.class_key, ref.name, arguments=ref.arguments, bases_only=True)
        if ref.lookup == "receiver":
            class_key, via = self._receiver_class(state, scope, ref.receiver)
            if via == "type_name":
                state.basis_override[id(ref)] = "type"  # as certain as a scope lookup
            return self._members(state, class_key, ref.name, arguments=ref.arguments) if class_key else []
        if ref.lookup == "new":
            classes = self._type_keys(state, ref.name)
            return self._constructor_or_class(state, classes[0], ref.arguments) if len(classes) == 1 else []
        if ref.lookup in ("this_constructor", "super_constructor"):
            owner = scope.class_key
            if ref.lookup == "super_constructor":
                owner = state.superclass.get(owner)
            return self._constructor_or_class(state, owner, ref.arguments) if owner else []
        return []

    @staticmethod
    def _type_keys(state, name) -> list[str]:
        return list(state.types.get(name, []))

    def prepare(self, state) -> None:
        """Turn recorded base-type names into same-file class keys; other bases are dropped."""
        def resolve(name):
            keys = self._type_keys(state, name)
            return keys[0] if len(keys) == 1 else None

        for class_key, names in list(state.bases.items()):
            state.bases[class_key] = [key for key in map(resolve, names) if key]
        for class_key, name in list(state.superclass.items()):
            if (key := resolve(name)) is not None:
                state.superclass[class_key] = key
            else:
                del state.superclass[class_key]

    def _constructor_or_class(self, state, class_key, arguments) -> list[str]:
        constructors = state.constructors.get(class_key, [])
        if not constructors:
            return [class_key]  # the implicit default constructor
        return self._by_arity(state, constructors, arguments)

    def _receiver_class(self, state, scope, receiver) -> tuple[str | None, str]:
        """The class a receiver denotes, and how: through a variable's declared type, or a type name."""
        via = "declared_type"
        if receiver.startswith("this."):
            type_name = self._field_type(state, scope.class_key, receiver[5:])
        else:
            type_name = state.variables.get(scope.callable_key, {}).get(receiver) \
                or self._field_type(state, scope.class_key, receiver)
            if type_name is None and receiver[:1].isupper():
                # A static call through a type name, e.g. Helper.run(). Lower-case receivers are
                # variables of unknown type (lambda parameters, `var` locals) and never link by name.
                type_name, via = receiver, "type_name"
        keys = self._type_keys(state, type_name) if type_name else []
        return (keys[0] if len(keys) == 1 else None), via

    @staticmethod
    def _field_type(state, class_key, name) -> str | None:
        seen = set()
        current = class_key
        while current and current not in seen:
            seen.add(current)
            if name in state.variables.get(current, {}):
                return state.variables[current][name]
            current = state.outer.get(current)
        return None


# --- Scala -----------------------------------------------------------------------------------

# Objects are singletons: like the JVM, their qualified name ends in `$`, so a companion object
# never collides with its class. Traits are the Scala counterpart of interfaces.
_SCALA_TEMPLATES = {
    "class_definition": (EntityType.CLASS, "class"),
    "object_definition": (EntityType.CLASS, "object"),
    "trait_definition": (EntityType.INTERFACE, "trait"),
    "enum_definition": (EntityType.CLASS, "enum"),
}
_SCALA_FUNCTIONS = ("function_definition", "function_declaration")
_SCALA_VARIABLES = ("val_definition", "var_definition", "val_declaration", "var_declaration")
_SCALA_COMMENTS = {"comment", "block_comment"}


def _scala_type_name(node: Any) -> str | None:
    """A type's name without type arguments (dotted when qualified); None for other type forms."""
    while node is not None and node.type in ("generic_type", "compound_type", "applied_constructor_type"):
        if node.type == "generic_type":
            node = node.child_by_field_name("type")
        elif node.type == "compound_type":
            node = node.child_by_field_name("base")
        else:
            node = next((c for c in node.named_children if c.type != "arguments"), None)
    if node is None or node.type not in ("type_identifier", "stable_type_identifier"):
        return None
    return _name(text_of(node))


def _scala_argument_count(arguments: Any) -> int | None:
    if arguments is None:
        return None
    if arguments.type == "block":  # f { ... } passes the block as the only argument
        return 1
    return sum(1 for child in arguments.named_children if child.type not in _SCALA_COMMENTS)


def _scala_is_super(node: Any) -> bool:
    if node.type == "generic_function":  # super[Trait]
        node = node.child_by_field_name("function")
    return node is not None and node.type == "identifier" and text_of(node) == "super"


def _scala_receiver_is_simple(node: Any) -> bool:
    while node.type == "field_expression":
        node = node.child_by_field_name("value")
    return node.type == "identifier" or _scala_is_super(node)


def _scala_header_broken(node: Any) -> bool:
    """A definition whose name, parameters, parents or type do not parse. Unlike Java, a missing
    parameter list is legal (`def size: Int`), so any error outside the body counts."""
    if node.child_by_field_name("name") is None:
        return True
    return any(child.type == "ERROR" or child.is_missing or child.has_error
               for index, child in enumerate(node.children) if node.field_name_for_child(index) != "body")


def _scala_span(node: Any) -> Span:
    """A declaration's span without trailing blank lines, which indentation-based (Scala 3)
    definitions include in their node."""
    span = span_of(node)
    text = node.text or b""
    trailing = len(text) - len(text.rstrip())
    if not trailing:
        return span
    kept = text[:len(text) - trailing]
    return Span(span.start_byte, span.end_byte - trailing, span.start_line,
                span.start_line + kept.count(b"\n"))


def _scala_parameters(parameter_list: Any) -> list[Any]:
    return [p for p in parameter_list.named_children if p.type in ("parameter", "class_parameter")]


class ScalaAnalyzer(TreeSitterAnalyzer):
    """Scala 2 and the common Scala 3 forms (enums, top-level definitions, `as`/`*` imports).

    Calls are explicit applications (`f(x)`, `a.f { ... }`, `new C(...)`, `C(...)`) and
    alphanumeric infix applications (`xs foreach f`). Symbolic operators and parameterless
    member selections are not reported: without types they cannot be told from arithmetic
    or field access.
    """

    language = "scala"
    extractor = "tree-sitter-scala"
    analyzer_version = "stacksniffer-scala/1"
    capabilities = AnalyzerCapabilities({
        Capability.DECLARATIONS: S,
        Capability.IMPORTS: S,
        Capability.CALLS: P,
        Capability.INHERITANCE: P,
        Capability.INTERFACES: P,  # traits
        Capability.DEPENDENCIES: U,  # build.sbt is not interpreted
        Capability.DECORATORS: S,  # annotations
    })

    def __init__(self, adapter: TreeSitterParserAdapter | None = None):
        super().__init__(adapter or TreeSitterParserAdapter("scala", "tree_sitter_scala", "tree-sitter-scala"))

    def visit(self, node, scope, state, builder):
        kind = node.type
        if kind == "ERROR" and node.parent is not None:
            return None  # a broken region: names and extents inside it are not trustworthy
        if node.parent is not None and node.parent.type == "ERROR" and node.start_point[1] != 0:
            # When recovery turns the whole file into an ERROR node, only definitions starting at
            # column 0 are known to be top level; indented ones belonged to a broken enclosing one.
            return None
        if kind == "package_clause":
            return self._package(node, scope, state)
        if kind == "import_declaration":
            self._import(node, builder)
            return None
        if kind in _SCALA_TEMPLATES:
            return self._template(node, scope, state, builder)
        if kind in _SCALA_FUNCTIONS:
            return self._function(node, scope, state, builder)
        if kind in _SCALA_VARIABLES:
            self._variables(node, scope, state)
        elif kind == "lambda_expression":
            parameters = node.child_by_field_name("parameters")
            if parameters is not None:
                state.bound[scope.namespace].update(text_of(n) for n in walk(parameters) if n.type == "identifier")
        elif kind == "call_expression":
            self._call(node, scope, state)
        elif kind == "infix_expression":
            operator = node.child_by_field_name("operator")
            if operator is not None and operator.type == "identifier":  # `xs foreach f`, never `a + b`
                self._member_call(node.child_by_field_name("left"), _name(text_of(operator)), span_of(node), 1,
                                  scope, state)
        elif kind == "instance_expression":
            self._instance(node, scope, state)
        return scope

    # -- declarations -----------------------------------------------------------------------

    @staticmethod
    def _package(node, scope, state):
        name = _name(text_of(node.child_by_field_name("name")))
        outer = scope.qualified_name or state.package
        qualified = f"{outer}.{name}" if outer else name
        body = node.child_by_field_name("body")
        if body is None:  # `package a.b` applies to the rest of the file
            state.package = qualified
            return None
        return _Scope(scope.key, "file", qualified, None, None, scope.namespace, None)

    def _import(self, node, builder):
        path: list[str] = []

        def emit(name: str, **metadata) -> None:
            target = builder.external("import", name, span_of(node))
            builder.relationship(RelationshipType.IMPORTS, builder.file_key, target, span_of(node),
                                 certainty=Certainty.EXACT,
                                 metadata={"module": name, "wildcard": name.endswith(".*"), **metadata,
                                           "resolution": "unresolved"})

        # One statement may import several paths (`import a.B, c.D`) and several selectors of one.
        for index, child in enumerate(node.children):
            if node.field_name_for_child(index) == "path":
                if child.is_named:  # the `.` separators carry the field too
                    path.append(text_of(child))
            elif child.type == "namespace_wildcard":
                if text_of(child) in ("_", "*"):
                    emit(".".join(path) + ".*")
                path = []
            elif child.type == "namespace_selectors":
                prefix = ".".join(path)
                for selector in child.named_children:
                    if selector.type == "identifier":
                        emit(f"{prefix}.{text_of(selector)}")
                    elif selector.type in ("arrow_renamed_identifier", "as_renamed_identifier"):
                        alias = text_of(selector.child_by_field_name("alias"))
                        if alias != "_":  # `X => _` hides X instead of importing it
                            emit(f"{prefix}.{text_of(selector.child_by_field_name('name'))}", alias=alias)
                    elif selector.type == "namespace_wildcard" and text_of(selector) in ("_", "*"):
                        emit(f"{prefix}.*")
                path = []
            elif child.type == "," and path:
                emit(".".join(path))
                path = []
        if path:
            emit(".".join(path))

    @staticmethod
    def _annotations(node) -> list[str]:
        names = []
        for child in node.children:
            if child.type == "annotation":
                name = _scala_type_name(child.child_by_field_name("name"))
                if name:
                    names.append(name)
        return names

    @staticmethod
    def _parameter_types(lists) -> tuple[str, tuple[float, float] | None]:
        """The signature suffix over every parameter list, and the first list's arity range."""
        suffix, arity = "", None
        for number, parameter_list in enumerate(lists):
            parameters = _scala_parameters(parameter_list)
            suffix += "(" + ",".join(_name(text_of(p.child_by_field_name("type"))) for p in parameters) + ")"
            if number == 0:
                repeated = any((t := p.child_by_field_name("type")) is not None
                               and t.type == "repeated_parameter_type" for p in parameters)
                required = sum(1 for p in parameters if p.child_by_field_name("default_value") is None)
                arity = (required - repeated, math.inf if repeated else len(parameters))
        return suffix, arity

    @staticmethod
    def _bind_parameters(owner, lists, state):
        for parameter_list in lists:
            for parameter in _scala_parameters(parameter_list):
                name = text_of(parameter.child_by_field_name("name"))
                type_name = _scala_type_name(parameter.child_by_field_name("type"))
                if type_name:
                    state.variables[owner][name] = type_name
                else:
                    state.bound[owner].add(name)

    def _template(self, node, scope, state, builder):
        if _scala_header_broken(node):
            return None
        name = text_of(node.child_by_field_name("name")).strip()
        entity_type, kind = _SCALA_TEMPLATES[node.type]
        if any(child.type == "case" for child in node.children):
            kind = f"case {kind}"
        own = f"{name}$" if node.type == "object_definition" else name
        if scope.kind == "callable":
            qualified = f"{scope.qualified_name}.<local>.{own}"
        else:
            qualified = self._qualify(scope, own, state.package)
        metadata: dict[str, Any] = {"kind": kind}
        if annotations := self._annotations(node):
            metadata["annotations"] = annotations
        key = builder.entity(entity_type, name, _scala_span(node), parent_key=scope.key,
                             qualified_name=qualified, metadata=metadata)
        state.classes.add(key)
        (state.objects if node.type == "object_definition" else state.types)[name].append(key)
        if node.type == "trait_definition":
            state.traits.add(key)
        if scope.class_key:
            state.outer[key] = scope.class_key
        state.parent_namespace[key] = scope.namespace

        if node.type in ("class_definition", "enum_definition"):
            lists = [n for n in node.children_by_field_name("class_parameters") if n.type == "class_parameters"]
            _, arity = self._parameter_types(lists)
            state.arity[key] = arity or (0, 0)  # the primary constructor
            self._bind_parameters(key, lists, state)

        extend = node.child_by_field_name("extend")
        if extend is not None:
            parents = [child for index, child in enumerate(extend.children)
                       if child.is_named and extend.field_name_for_child(index) == "type"]
            for number, parent in enumerate(parents):
                # The first parent is the superclass template; later ones are mixins. A trait's
                # parents are all traits it extends.
                relationship = (RelationshipType.EXTENDS if number == 0 or node.type == "trait_definition"
                                else RelationshipType.IMPLEMENTS)
                self._parent(relationship, key, parent, scope, state, superclass=number == 0)
            arguments = extend.child_by_field_name("arguments")
            if arguments is not None and parents and (parent_name := _scala_type_name(parents[0])):
                # `extends Base(x)` invokes Base's constructor.
                start, end = span_of(parents[0]), span_of(arguments)
                span = Span(start.start_byte, end.end_byte, start.start_line, end.end_line)
                state.references.append(_Reference(
                    RelationshipType.CALLS, key, span, "call", parent_name,
                    lookup="new" if "." not in parent_name else "none", name=parent_name, scope=scope,
                    arguments=_scala_argument_count(arguments)))
        return _Scope(key, "class", qualified, key, None, key, None)

    @staticmethod
    def _parent(relationship, source_key, type_node, scope, state, *, superclass=False):
        name = _scala_type_name(type_node)
        if not name:
            return
        # Only simple names can match a same-file declaration; qualified names stay unresolved.
        lookup = "type" if "." not in name else "none"
        state.references.append(_Reference(relationship, source_key, span_of(type_node), "type", name,
                                           lookup=lookup, name=name, scope=scope))
        if lookup == "type":
            state.bases[source_key].append(name)  # names until prepare() resolves them
            if superclass:
                state.superclass[source_key] = name

    def _function(self, node, scope, state, builder):
        if _scala_header_broken(node):
            return None
        name = text_of(node.child_by_field_name("name")).strip()
        # type parameters (`def f[T](x: T)`) share the field; only value parameter lists count
        lists = [n for n in node.children_by_field_name("parameters") if n.type == "parameters"]
        suffix, arity = self._parameter_types(lists)
        signature = name + suffix
        constructor = name == "this"
        body_owner = node.parent.parent if node.parent is not None else None
        anonymous = node.parent.type == "template_body" and body_owner is not None \
            and body_owner.type == "instance_expression"
        if anonymous:
            base = scope.qualified_name or state.package
            qualified = f"{base}.<anonymous>.{signature}" if base else f"<anonymous>.{signature}"
            class_key = f"anonymous@{node.parent.id}"
            if class_key not in state.classes:
                state.classes.add(class_key)
                state.parent_namespace[class_key] = scope.namespace
                state.outer.setdefault(class_key, scope.class_key)
        elif scope.kind == "class":
            qualified, class_key = self._qualify(scope, signature), scope.class_key
        else:
            qualified, class_key = self._qualify(scope, signature, state.package), None
        metadata: dict[str, Any] = {"signature": signature, "constructor": constructor}
        if node.type == "function_declaration":
            metadata["abstract"] = True
        if annotations := self._annotations(node):
            metadata["annotations"] = annotations
        key = builder.entity(EntityType.METHOD if class_key else EntityType.FUNCTION, name, _scala_span(node),
                             parent_key=scope.key, qualified_name=qualified, metadata=metadata)
        if arity is not None:
            state.arity[key] = arity  # a parameterless `def x` has none: it is never applied
        if class_key and constructor:
            state.constructors[class_key].append(key)
        elif class_key:
            state.members[class_key][name].append(key)
        else:
            state.names[scope.namespace][name].append(_Declaration(key, span_of(node).start_line, True))
        state.parent_namespace[key] = class_key if anonymous else scope.namespace
        self._bind_parameters(key, lists, state)
        return _Scope(key, "callable", qualified, class_key or scope.class_key, key, key, None)

    @staticmethod
    def _variables(node, scope, state):
        pattern = node.child_by_field_name("pattern")
        if pattern is None or pattern.type != "identifier":
            return
        name = text_of(pattern)
        type_name = _scala_type_name(node.child_by_field_name("type"))
        if type_name:
            state.variables[scope.namespace][name] = type_name
        else:
            state.bound[scope.namespace].add(name)  # inferred type: known to exist, type unknown

    # -- references -------------------------------------------------------------------------

    def _call(self, node, scope, state):
        function = node.child_by_field_name("function")
        if function is None or function.type == "call_expression":
            return  # a further argument list of a curried application; the innermost call reports it
        if function.type == "generic_function":  # f[T](x)
            function = function.child_by_field_name("function")
        arguments = _scala_argument_count(node.child_by_field_name("arguments"))
        span = span_of(node)
        if function.type == "identifier":
            name = _name(text_of(function))
            source = scope.callable_key or scope.key
            lookup = "this_constructor" if name == "this" else "unqualified"
            state.references.append(_Reference(RelationshipType.CALLS, source, span, "call", name, lookup=lookup,
                                               name=name, scope=scope, arguments=arguments))
        elif function.type == "field_expression":
            self._member_call(function.child_by_field_name("value"),
                              _name(text_of(function.child_by_field_name("field"))), span, arguments, scope, state)

    def _member_call(self, receiver, name, span, arguments, scope, state):
        if receiver is None or not name:
            return
        source = scope.callable_key or scope.key
        text = _name(text_of(receiver))
        display = f"{text if _scala_receiver_is_simple(receiver) else EXPRESSION}.{name}"
        receiver_name = None
        if receiver.type == "identifier" and text == "this":
            lookup = "this"
        elif _scala_is_super(receiver):
            lookup = "super"
        elif receiver.type == "identifier":
            lookup, receiver_name = "receiver", text
        elif receiver.type == "field_expression" and text.startswith("this.") and text.count(".") == 1:
            lookup, receiver_name = "receiver", text
        elif receiver.type == "instance_expression" and (type_name := self._instance_type(receiver)[0]):
            lookup, receiver_name = "instance", type_name  # new Local().run()
        else:
            lookup = "none"
        state.references.append(_Reference(RelationshipType.CALLS, source, span, "call", display, lookup=lookup,
                                           name=name, scope=scope, arguments=arguments, receiver=receiver_name))

    @staticmethod
    def _instance_type(node) -> tuple[str | None, int]:
        type_node = next((c for c in node.named_children if c.type not in ("arguments", "template_body")), None)
        arguments = node.child_by_field_name("arguments")
        if type_node is not None and type_node.type == "compound_type":  # new A(x) with B
            type_node = type_node.child_by_field_name("base")
        if type_node is not None and type_node.type == "applied_constructor_type":
            arguments = next((c for c in type_node.named_children if c.type == "arguments"), arguments)
        return _scala_type_name(type_node), _scala_argument_count(arguments) or 0

    def _instance(self, node, scope, state):
        type_name, arguments = self._instance_type(node)
        if not type_name:
            return
        # `new T { ... }` instantiates an anonymous subclass, so T may be a trait (as a Java
        # anonymous class may implement an interface).
        anonymous = any(child.type == "template_body" for child in node.named_children)
        state.references.append(_Reference(
            RelationshipType.CALLS, scope.callable_key or scope.key, span_of(node), "call", type_name,
            lookup="new" if "." not in type_name else "none", name=type_name, scope=scope, arguments=arguments,
            receiver="anonymous" if anonymous else None))

    # -- resolution -------------------------------------------------------------------------

    def candidates(self, ref, state):
        scope = ref.scope
        if ref.lookup == "type":
            return list(state.types.get(ref.name, []))
        if ref.lookup == "unqualified":
            found, how = self._unqualified(state, scope, ref.name, ref.arguments)
            if how:
                state.basis_override[id(ref)] = how
            return found
        if ref.lookup == "this":
            return self._members(state, scope.class_key, ref.name, arguments=ref.arguments)
        if ref.lookup == "super":
            return self._members(state, scope.class_key, ref.name, arguments=ref.arguments, bases_only=True)
        if ref.lookup == "receiver":
            class_key, how = self._receiver_class(state, scope, ref.receiver)
            state.basis_override[id(ref)] = how
            return self._members(state, class_key, ref.name, arguments=ref.arguments) if class_key else []
        if ref.lookup == "instance":
            classes = self._instantiable(state, ref.receiver)
            return self._members(state, classes[0], ref.name, arguments=ref.arguments) if len(classes) == 1 else []
        if ref.lookup == "new":
            classes = (list(state.types.get(ref.name, [])) if ref.receiver == "anonymous"
                       else self._instantiable(state, ref.name))
            return self._constructors(state, classes[0], ref.arguments) if len(classes) == 1 else []
        if ref.lookup == "this_constructor" and scope.class_key:
            return [key for key in self._constructors(state, scope.class_key, ref.arguments) if key != ref.source_key]
        return []

    @staticmethod
    def _instantiable(state, name) -> list[str]:
        return [key for key in state.types.get(name, []) if key not in state.traits]

    @staticmethod
    def _constructors(state, class_key, arguments) -> list[str]:
        """Auxiliary constructors and the primary one (the class itself) that accept the arguments."""
        def fits(key):
            low, high = state.arity.get(key, (0, math.inf))
            return arguments is None or low <= arguments <= high
        return [key for key in state.constructors.get(class_key, []) if fits(key)] + \
            ([class_key] if fits(class_key) else [])

    def _unqualified(self, state, scope, name, arguments) -> tuple[list[str], str | None]:
        """Scala name lookup: local definitions, then members of each enclosing template
        (inherited ones included), then top-level definitions; parameters and values shadow."""
        namespace = scope.namespace
        while namespace is not None:
            if namespace in state.classes:
                found = self._inherited(state, namespace, name, arguments, set())
                if found:
                    return found, "unqualified"
            else:
                declarations = state.names[namespace].get(name)
                if declarations:
                    return self._by_arity(state, [d.key for d in declarations], arguments), "lexical"
                if name in state.bound[namespace] or name in state.variables.get(namespace, {}):
                    return [], None
            namespace = state.parent_namespace.get(namespace)
        # Not a function in scope: `Name(...)` applies a same-file companion object or class.
        objects = state.objects.get(name, [])
        if len(objects) == 1 and state.members[objects[0]].get("apply"):
            return self._by_arity(state, state.members[objects[0]]["apply"], arguments), "type"
        classes = self._instantiable(state, name)
        if len(classes) == 1:
            return self._constructors(state, classes[0], arguments), "new"
        return [], None

    def _receiver_class(self, state, scope, receiver) -> tuple[str | None, str]:
        """The template a receiver denotes, and how: a declared type ("receiver") or an object name ("type")."""
        if receiver.startswith("this."):
            type_name = state.variables.get(scope.class_key, {}).get(receiver[5:])
        else:
            type_name, shadowed = self._variable_type(state, scope.namespace, receiver)
            if type_name is None and not shadowed and receiver[:1].isupper():
                # Name.member(...) selects from the object Name, like a Java static call.
                objects = state.objects.get(receiver, [])
                return (objects[0] if len(objects) == 1 else None), "type"
        keys = list(state.types.get(type_name, [])) if type_name else []
        return (keys[0] if len(keys) == 1 else None), "receiver"

    @staticmethod
    def _variable_type(state, namespace, name) -> tuple[str | None, bool]:
        """The declared type of the nearest binding of ``name``; (None, True) when it has none."""
        while namespace is not None:
            if name in state.bound[namespace]:
                return None, True
            if name in state.variables.get(namespace, {}):
                return state.variables[namespace][name], False
            namespace = state.parent_namespace.get(namespace)
        return None, False

    def prepare(self, state) -> None:
        """Turn recorded parent names into same-file class/trait keys; other parents are dropped."""
        def resolve(name):
            keys = state.types.get(name, [])
            return keys[0] if len(keys) == 1 else None

        for class_key, names in list(state.bases.items()):
            state.bases[class_key] = [key for key in map(resolve, names) if key]
        for class_key, name in list(state.superclass.items()):
            if (key := resolve(name)) is not None:
                state.superclass[class_key] = key
            else:
                del state.superclass[class_key]


# --- Go --------------------------------------------------------------------------------------

# Names that make `name(x)` a conversion rather than a call.
_GO_PREDECLARED_TYPES = frozenset({
    "bool", "byte", "rune", "string", "error", "any", "uintptr", "int", "int8", "int16", "int32", "int64",
    "uint", "uint8", "uint16", "uint32", "uint64", "float32", "float64", "complex64", "complex128",
})
# Method sets of standard-library interfaces commonly embedded in repository interfaces, so an
# implicit implementation can still be decided: (import path, name) -> [(method, parameters)].
_GO_KNOWN_INTERFACES = {
    ("", "error"): [("Error", 0)],
    ("fmt", "Stringer"): [("String", 0)],
    ("io", "Reader"): [("Read", 1)],
    ("io", "Writer"): [("Write", 1)],
    ("io", "Closer"): [("Close", 0)],
    ("sort", "Interface"): [("Len", 0), ("Less", 2), ("Swap", 2)],
}
_GO_VERSION_SUFFIX = re.compile(r"^v\d+$")


def _go_type_name(node: Any) -> str | None:
    """A named type without pointers or type arguments (`pkg.T` when qualified); None otherwise."""
    while node is not None and node.type in ("pointer_type", "generic_type", "parenthesized_type"):
        node = node.child_by_field_name("type") if node.type == "generic_type" else \
            next((c for c in node.named_children), None)
    if node is None or node.type not in ("type_identifier", "qualified_type"):
        return None
    return _name(text_of(node))


def _go_parameters(parameter_list: Any) -> tuple[list[tuple[str, str | None]], tuple[float, float]]:
    """(name, type name) for every declared parameter, and the arity range."""
    bindings, count, variadic = [], 0, False
    for declaration in parameter_list.named_children if parameter_list is not None else []:
        if declaration.type not in ("parameter_declaration", "variadic_parameter_declaration"):
            continue
        type_name = _go_type_name(declaration.child_by_field_name("type"))
        names = declaration.children_by_field_name("name")
        if declaration.type == "variadic_parameter_declaration":
            variadic = True
        else:
            count += max(1, len(names))
        bindings.extend((text_of(n), type_name) for n in names)
    return bindings, (count, math.inf if variadic else count)


def _go_import_name(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    if len(parts) > 1 and _GO_VERSION_SUFFIX.match(parts[-1]):
        parts.pop()  # example.com/lib/v2 is package lib
    return parts[-1] if parts else path


def _go_chain_is_simple(node: Any) -> bool:
    while node.type == "selector_expression":
        node = node.child_by_field_name("operand")
    return node.type == "identifier"


class GoAnalyzer(TreeSitterAnalyzer):
    """Go packages: named types, interfaces, functions and methods; embedding as EXTENDS; implicit
    interface satisfaction as IMPLEMENTS (same file, by method name and parameter count).

    A method belongs to its receiver type, which may be declared in another file of the package;
    its parent is then the file. Conversions (`T(x)`) and composite literals are not calls.
    """

    language = "go"
    extractor = "tree-sitter-go"
    analyzer_version = "stacksniffer-go/1"
    capabilities = AnalyzerCapabilities({
        Capability.DECLARATIONS: S,
        Capability.IMPORTS: S,
        Capability.CALLS: P,
        Capability.INHERITANCE: P,  # embedding
        Capability.INTERFACES: P,  # implicit, same file
        Capability.DEPENDENCIES: U,  # go.mod is not interpreted
        Capability.DECORATORS: U,  # Go has none
    })

    def __init__(self, adapter: TreeSitterParserAdapter | None = None):
        super().__init__(adapter or TreeSitterParserAdapter("go", "tree_sitter_go", "tree-sitter-go"))

    def visit(self, node, scope, state, builder):
        kind = node.type
        if kind == "source_file":
            self._index_types(node, state, builder)
            return scope
        if kind == "ERROR" and node.parent is not None:
            return None
        if kind == "package_clause":
            name = next((c for c in node.named_children if c.type == "package_identifier"), None)
            state.package = text_of(name) or None
            return None
        if kind == "import_declaration":
            for spec in (s for s in walk(node) if s.type == "import_spec"):
                self._import(spec, builder, state)
            return None
        if kind == "type_spec":
            return self._type(node, scope, state, builder)
        if kind == "type_alias":
            return None  # another name for an existing type, not a declaration
        if kind in ("function_declaration", "method_declaration"):
            return self._function(node, scope, state, builder)
        if kind == "func_literal":
            # Closures are not declarations: their calls belong to the enclosing function.
            parameters, _ = _go_parameters(node.child_by_field_name("parameters"))
            self._bind(state, scope.namespace, parameters)
        elif kind in ("var_spec", "const_spec"):
            type_name = _go_type_name(node.child_by_field_name("type"))
            self._bind(state, scope.namespace, [(text_of(n), type_name) for n in node.children_by_field_name("name")])
        elif kind in ("short_var_declaration", "range_clause", "receive_statement"):
            left = node.child_by_field_name("left")
            names = [text_of(n) for n in walk(left) if n.type == "identifier"] if left is not None else []
            self._bind(state, scope.namespace, [(n, None) for n in names])  # inferred types
        elif kind == "call_expression":
            self._call(node, scope, state)
        elif kind == "type_conversion_expression":
            self._generic_call(node, scope, state)
        return scope

    # -- declarations -----------------------------------------------------------------------

    def _index_types(self, root, state, builder):
        """Record the file's top-level types first: methods may precede their receiver type."""
        state.go_interfaces = {}  # interface key -> (own [(method, arity)], embedded type names)
        state.go_fields = defaultdict(dict)  # type key -> field -> type name
        state.go_type_nodes = {}  # type key -> name node (evidence for implicit IMPLEMENTS)
        state.go_imports = {}  # package name in this file -> import path
        state.go_receivers = {}  # method key -> (receiver name, receiver type key)
        for declaration in root.named_children:
            if declaration.type != "type_declaration":
                continue
            for spec in declaration.named_children:
                if spec.type != "type_spec" or _scala_header_broken(spec):
                    continue
                name = text_of(spec.child_by_field_name("name"))
                definition = spec.child_by_field_name("type")
                interface = definition is not None and definition.type == "interface_type"
                entity = EntityType.INTERFACE if interface else EntityType.CLASS
                key = f"{entity.value.lower()}:{builder.path}::{self._qualify_top(state, root, name)}"
                state.types[name].append(key)
                state.classes.add(key)
                if interface:
                    state.traits.add(key)

    @staticmethod
    def _qualify_top(state, root, name) -> str:
        if state.package is None:
            clause = next((c for c in root.named_children if c.type == "package_clause"), None)
            package = next((c for c in clause.named_children if c.type == "package_identifier"), None) \
                if clause is not None else None
            state.package = text_of(package) or None
        return f"{state.package}.{name}" if state.package else name

    def _import(self, spec, builder, state):
        path_node = spec.child_by_field_name("path")
        path = text_of(path_node).strip('"`')
        if not path:
            return
        alias_node = spec.child_by_field_name("name")
        alias = text_of(alias_node) if alias_node is not None else None
        metadata = {"module": path, "resolution": "unresolved"}
        if alias:
            metadata["alias"] = alias
        if alias not in ("_", "."):
            state.go_imports[alias or _go_import_name(path)] = path
        target = builder.external("import", path, span_of(path_node))
        builder.relationship(RelationshipType.IMPORTS, builder.file_key, target, span_of(spec),
                             certainty=Certainty.EXACT, metadata=metadata)

    def _type(self, node, scope, state, builder):
        if _scala_header_broken(node):
            return None
        name = text_of(node.child_by_field_name("name"))
        definition = node.child_by_field_name("type")
        interface = definition is not None and definition.type == "interface_type"
        declaration = node.parent
        # A lone `type T ...` spans the keyword; a spec inside `type ( ... )` spans itself.
        span_node = declaration if declaration is not None and declaration.type == "type_declaration" \
            and len([c for c in declaration.named_children if c.type in ("type_spec", "type_alias")]) == 1 else node
        if scope.kind == "callable":
            qualified = f"{scope.qualified_name}.<local>.{name}"
        else:
            qualified = f"{state.package}.{name}" if state.package else name
        kind = "interface" if interface else ("struct" if definition is not None and definition.type == "struct_type"
                                              else "named")
        key = builder.entity(EntityType.INTERFACE if interface else EntityType.CLASS, name, span_of(span_node),
                             parent_key=scope.key, qualified_name=qualified, metadata={"kind": kind})
        if key not in state.classes:  # local types are not in the top-level index
            state.types[name].append(key)
            state.classes.add(key)
            if interface:
                state.traits.add(key)
        state.go_type_nodes[key] = node.child_by_field_name("name")
        if interface:
            self._interface(key, definition, scope, state, builder)
        elif definition is not None and definition.type == "struct_type":
            self._struct(key, definition, scope, state)
        return None

    def _interface(self, key, definition, scope, state, builder):
        own, embedded = [], []
        for element in definition.named_children:
            if element.type == "method_elem":
                method = text_of(element.child_by_field_name("name"))
                _, arity = _go_parameters(element.child_by_field_name("parameters"))
                own.append((method, arity[0]))
                method_key = builder.entity(
                    EntityType.METHOD, method, span_of(element), parent_key=key,
                    qualified_name=f"{key.split('::', 1)[1]}.{method}", metadata={"abstract": True})
                state.members[key][method].append(method_key)
                state.arity[method_key] = arity
            elif element.type == "type_elem":
                for type_node in element.named_children:
                    type_name = _go_type_name(type_node)
                    if type_name:
                        embedded.append(type_name)
                        self._parent(key, type_name, type_node, scope, state)
        state.go_interfaces[key] = (own, embedded)

    def _struct(self, key, definition, scope, state):
        fields = next((c for c in definition.named_children if c.type == "field_declaration_list"), None)
        for field in fields.named_children if fields is not None else []:
            if field.type != "field_declaration":
                continue
            type_node = field.child_by_field_name("type")
            type_name = _go_type_name(type_node)
            names = field.children_by_field_name("name")
            if not names and type_name:  # an embedded field promotes its methods
                self._parent(key, type_name, type_node, scope, state)
                state.go_fields[key][type_name.rsplit(".", 1)[-1]] = type_name
            for name in names:
                state.go_fields[key][text_of(name)] = type_name

    @staticmethod
    def _parent(key, type_name, type_node, scope, state):
        lookup = "type" if "." not in type_name else "none"
        state.references.append(_Reference(RelationshipType.EXTENDS, key, span_of(type_node), "type", type_name,
                                           lookup=lookup, name=type_name, scope=scope,
                                           metadata={"embedded": True}))
        if lookup == "type":
            state.bases[key].append(type_name)  # names until prepare() resolves them

    def _function(self, node, scope, state, builder):
        if _scala_header_broken(node):
            return None
        name = text_of(node.child_by_field_name("name"))
        parameters, arity = _go_parameters(node.child_by_field_name("parameters"))
        receiver_name, receiver_type = None, None
        if node.type == "method_declaration":
            receivers, _ = _go_parameters(node.child_by_field_name("receiver"))
            receiver_list = node.child_by_field_name("receiver")
            declaration = next((c for c in receiver_list.named_children if c.type == "parameter_declaration"), None) \
                if receiver_list is not None else None
            receiver_type = _go_type_name(declaration.child_by_field_name("type")) if declaration is not None else None
            receiver_name = receivers[0][0] if receivers else None
        package = f"{state.package}." if state.package else ""
        if receiver_type:
            type_keys = [k for k in state.types.get(receiver_type, []) if k not in state.traits]
            owner = type_keys[0] if len(type_keys) == 1 else f"go-type:{receiver_type}"
            qualified = f"{package}{receiver_type}.{name}"
            entity_type, parent = EntityType.METHOD, (owner if len(type_keys) == 1 else scope.key)
        else:
            owner, qualified, entity_type, parent = None, f"{package}{name}", EntityType.FUNCTION, scope.key
        metadata: dict[str, Any] = {}
        if receiver_type:
            receiver_node = declaration.child_by_field_name("type")
            metadata["pointer_receiver"] = receiver_node is not None and receiver_node.type == "pointer_type"
        key = builder.entity(entity_type, name, span_of(node), parent_key=parent, qualified_name=qualified,
                             metadata=metadata)
        state.arity[key] = arity
        if owner:
            state.members[owner][name].append(key)
        else:
            state.names[scope.namespace][name].append(_Declaration(key, span_of(node).start_line, True))
        state.parent_namespace[key] = scope.namespace
        self._bind(state, key, parameters)
        if receiver_name and receiver_name != "_":
            state.variables[key][receiver_name] = receiver_type
            state.go_receivers[key] = (receiver_name, owner)
        return _Scope(key, "callable", qualified, owner, key, key, None)

    @staticmethod
    def _bind(state, namespace, bindings):
        for name, type_name in bindings:
            if not name or name == "_":
                continue
            if type_name:
                state.variables[namespace][name] = type_name
            else:
                state.bound[namespace].add(name)

    # -- references -------------------------------------------------------------------------

    def _call(self, node, scope, state):
        function = node.child_by_field_name("function")
        if function is None:
            return
        source = scope.callable_key or scope.key
        span = span_of(node)
        arguments = node.child_by_field_name("arguments")
        count = sum(1 for c in arguments.named_children if c.type != "comment") if arguments is not None else None
        if function.type == "identifier":
            name = _name(text_of(function))
            if name in _GO_PREDECLARED_TYPES or self._is_type(state, name):
                return  # a conversion
            state.references.append(_Reference(RelationshipType.CALLS, source, span, "call", name,
                                               lookup="lexical", name=name, scope=scope, arguments=count))
        elif function.type == "selector_expression":
            operand = function.child_by_field_name("operand")
            field = _name(text_of(function.child_by_field_name("field")))
            simple = _go_chain_is_simple(operand)
            display = f"{_name(text_of(operand)) if simple else EXPRESSION}.{field}"
            lookup, receiver = "none", None
            if operand.type == "identifier":
                lookup, receiver = "receiver", text_of(operand)
            elif operand.type == "selector_expression" and simple and \
                    operand.child_by_field_name("operand").type == "identifier":
                lookup, receiver = "receiver", _name(text_of(operand))  # recv.field.Method()
            state.references.append(_Reference(RelationshipType.CALLS, source, span, "call", display, lookup=lookup,
                                               name=field, scope=scope, arguments=count, receiver=receiver))

    def _generic_call(self, node, scope, state):
        """`F[T](x)` parses as a conversion to a generic type; it is a call when F is a function."""
        type_node = node.child_by_field_name("type")
        if type_node is None or type_node.type != "generic_type":
            return
        name = _name(text_of(type_node.child_by_field_name("type")))
        if not name or self._is_type(state, name):
            return
        state.references.append(_Reference(RelationshipType.CALLS, scope.callable_key or scope.key, span_of(node),
                                           "call", name, lookup="lexical", name=name, scope=scope))

    @staticmethod
    def _is_type(state, name) -> bool:
        return bool(state.types.get(name))

    # -- resolution -------------------------------------------------------------------------

    def candidates(self, ref, state):
        scope = ref.scope
        if ref.lookup == "type":
            return list(state.types.get(ref.name, []))
        if ref.lookup == "lexical":
            if self._shadowed(state, scope.namespace, ref.name):
                return []  # a function-typed variable or parameter
            return [d.key for d in state.names[scope_file(state, scope)].get(ref.name, [])]
        if ref.lookup == "receiver":
            owner, how = self._receiver_owner(state, scope, ref.receiver)
            if owner is None:
                return []
            state.basis_override[id(ref)] = how
            return self._members(state, owner, ref.name)
        return []

    def _receiver_owner(self, state, scope, receiver) -> tuple[str | None, str]:
        head, _, field = receiver.partition(".")
        receivers = getattr(state, "go_receivers", {})
        method_receiver, method_owner = receivers.get(scope.callable_key, (None, None))
        if head in getattr(state, "go_imports", {}) and not self._shadowed(state, scope.namespace, head):
            return None, "receiver"  # a package-qualified function
        if head == method_receiver and not field:
            return method_owner, "self"  # Go's receiver is its `this`
        if head == method_receiver:
            type_name = state.go_fields.get(method_owner, {}).get(field)  # recv.field.Method()
        elif field:
            return None, "receiver"
        else:
            type_name = self._declared_type(state, scope.namespace, head)
        keys = list(state.types.get(type_name, [])) if type_name else []
        return (keys[0] if len(keys) == 1 else None), "receiver"

    @staticmethod
    def _declared_type(state, namespace, name) -> str | None:
        while namespace is not None:
            if name in state.bound[namespace]:
                return None
            if name in state.variables.get(namespace, {}):
                return state.variables[namespace][name]
            namespace = state.parent_namespace.get(namespace)
        return None

    @staticmethod
    def _shadowed(state, namespace, name) -> bool:
        while namespace is not None and namespace in state.parent_namespace \
                and state.parent_namespace[namespace] is not None:
            if name in state.bound[namespace] or name in state.variables.get(namespace, {}):
                return True
            namespace = state.parent_namespace[namespace]
        return False

    def prepare(self, state) -> None:
        for class_key, names in list(state.bases.items()):
            resolved = []
            for name in names:
                keys = state.types.get(name, [])
                if len(keys) == 1:
                    resolved.append(keys[0])
            state.bases[class_key] = resolved

    def finish(self, state, builder) -> None:
        """Emit IMPLEMENTS for each same-file type whose method set covers a same-file interface."""
        interfaces = getattr(state, "go_interfaces", {})
        imports = {alias: path for alias, path in getattr(state, "go_imports", {}).items()}

        def required(key, seen) -> list[tuple[str, float]] | None:
            if key in seen:
                return []
            seen.add(key)
            own, embedded = interfaces[key]
            methods = list(own)
            for name in embedded:
                keys = [k for k in state.types.get(name, []) if k in interfaces]
                if len(keys) == 1:
                    inner = required(keys[0], seen)
                else:
                    package, _, local = name.rpartition(".")
                    known = _GO_KNOWN_INTERFACES.get((imports.get(package, package), local))
                    inner = list(known) if known is not None else None
                if inner is None:
                    return None  # an embedded interface whose methods are unknown
                methods += inner
            return methods

        def method_set(key, seen) -> dict[str, list[tuple[float, float]]]:
            if key in seen:
                return {}
            seen.add(key)
            found = {name: [state.arity.get(k, (0, math.inf)) for k in keys]
                     for name, keys in state.members.get(key, {}).items()}
            for base in state.bases.get(key, []):
                for name, arities in method_set(base, seen).items():
                    found.setdefault(name, arities)  # the shallower method wins, as in promotion
            return found

        for interface_key in sorted(interfaces):
            needs = required(interface_key, set())
            if not needs:
                continue  # empty, or not decidable from this file
            for type_key in sorted(k for k in state.classes if k.startswith("class:") and k in state.go_type_nodes):
                methods = method_set(type_key, set())
                if all(any(low <= arity <= high for low, high in methods.get(name, [])) for name, arity in needs):
                    builder.relationship(RelationshipType.IMPLEMENTS, type_key, interface_key,
                                         span_of(state.go_type_nodes[type_key]), certainty=Certainty.LOW,
                                         metadata={"resolution": "same_file", "resolution_basis": "method_set",
                                                   "implicit": True})


def scope_file(state, scope) -> str:
    """The package-level namespace of a scope: the file's own."""
    namespace = scope.namespace
    while state.parent_namespace.get(namespace) is not None:
        namespace = state.parent_namespace[namespace]
    return namespace


# --- JavaScript ------------------------------------------------------------------------------

_JS_FUNCTION_VALUES = ("arrow_function", "function_expression", "function", "generator_function")
_JS_CLASS_VALUES = ("class",)
_JS_JSDOC_PARAM = re.compile(r"@param\s+\{\s*\??([A-Za-z_$][\w$]*)\s*=?\s*\}\s*\[?([A-Za-z_$][\w$]*)")
_JS_JSDOC_TYPE = re.compile(r"@type\s+\{\s*\??([A-Za-z_$][\w$]*)\s*\}")


def _js_string(node: Any) -> str | None:
    if node is None or node.type not in ("string", "template_string"):
        return None
    fragments = [c for c in node.named_children if c.type == "string_fragment"]
    if node.type == "template_string" and any(c.type == "template_substitution" for c in node.named_children):
        return None  # not a static specifier
    return "".join(text_of(f) for f in fragments)


def _js_bound_names(node: Any) -> list[str]:
    """Identifiers a parameter or binding pattern introduces."""
    if node is None:
        return []
    if node.type in ("identifier", "shorthand_property_identifier_pattern"):
        return [text_of(node)]
    if node.type == "assignment_pattern":
        return _js_bound_names(node.child_by_field_name("left"))
    if node.type == "pair_pattern":
        return _js_bound_names(node.child_by_field_name("value"))
    if node.type in ("rest_pattern", "object_pattern", "array_pattern", "formal_parameters",
                     "object_assignment_pattern"):
        return [name for child in node.named_children for name in _js_bound_names(child)]
    return []


def _js_jsdoc(node: Any) -> str:
    """The `/** ... */` comment directly before a declaration (or its export/declaration wrapper)."""
    while node is not None and node.parent is not None and node.parent.type in (
            "export_statement", "lexical_declaration", "variable_declaration") and node.prev_named_sibling is None:
        node = node.parent
    previous = node.prev_named_sibling if node is not None else None
    if previous is None and node is not None and node.parent is not None and node.parent.type == "export_statement":
        previous = node.parent.prev_named_sibling
    if previous is not None and previous.type == "comment" and text_of(previous).startswith("/**") \
            and previous.end_point[0] + 1 >= node.start_point[0]:
        return text_of(previous)
    return ""


def _js_chain_is_simple(node: Any) -> bool:
    while node.type == "member_expression":
        node = node.child_by_field_name("object")
    return node.type in ("identifier", "this", "super")


class JavaScriptAnalyzer(TreeSitterAnalyzer):
    """ECMAScript modules and CommonJS. JavaScript has no declared types; JSDoc `@param {T} x` and
    `@type {T}` annotations supply them. Interfaces and decorators do not exist in the language."""

    language = "javascript"
    extractor = "tree-sitter-javascript"
    analyzer_version = "stacksniffer-javascript/1"
    capabilities = AnalyzerCapabilities({
        Capability.DECLARATIONS: S,
        Capability.IMPORTS: S,
        Capability.CALLS: P,
        Capability.INHERITANCE: P,
        Capability.INTERFACES: U,
        Capability.DEPENDENCIES: U,  # package.json is not interpreted
        Capability.DECORATORS: U,
    })

    def __init__(self, adapter: TreeSitterParserAdapter | None = None):
        super().__init__(adapter or TreeSitterParserAdapter("javascript", "tree_sitter_javascript",
                                                            "tree-sitter-javascript"))

    def visit(self, node, scope, state, builder):
        kind = node.type
        if kind == "ERROR" and node.parent is not None:
            return None
        if node.id in state.handled:
            return None  # an object-literal function already walked as a declaration
        if kind == "import_statement" or (kind == "export_statement" and node.child_by_field_name("source")):
            module = _js_string(node.child_by_field_name("source"))
            if module:
                self._import(builder, module, node)
            self._bind_imports(node, scope, state)
            return None
        if kind == "class_declaration":
            name = node.child_by_field_name("name")
            return self._class(node, text_of(name) if name is not None else "default", node, scope, state, builder)
        if kind in ("function_declaration", "generator_function_declaration"):
            name = node.child_by_field_name("name")
            return self._function(node, text_of(name) if name is not None else "default", node, scope, state,
                                  builder, method=False)
        if kind == "export_statement":
            value = node.child_by_field_name("value")
            if value is not None and value.type in _JS_FUNCTION_VALUES + _JS_CLASS_VALUES:  # export default <anon>
                handler = self._class if value.type in _JS_CLASS_VALUES else self._function
                extra = {} if value.type in _JS_CLASS_VALUES else {"method": False}
                named = value.child_by_field_name("name")
                child = handler(value, text_of(named) if named is not None else "default", value, scope, state,
                                builder, **extra)
                return None if child is None else self._walk_children_as(value, child, state, builder)
            return scope
        if kind == "variable_declarator":
            return self._declarator(node, scope, state, builder)
        if kind in ("method_definition", "field_definition") and node.parent is not None \
                and node.parent.type == "class_body":
            return self._member(node, scope, state, builder)
        if kind == "assignment_expression" and self._commonjs_export(node, scope, state, builder):
            return None
        if kind in ("arrow_function", "function_expression", "function", "generator_function"):
            return self._anonymous(node, scope, state)
        if kind in ("catch_clause", "for_in_statement"):
            pattern = node.child_by_field_name("parameter") or node.child_by_field_name("left")
            state.bound[scope.namespace].update(_js_bound_names(pattern))
        elif kind == "call_expression":
            self._call(node, scope, state, builder)
        elif kind == "new_expression":
            self._new(node, scope, state)
        return scope

    def _walk_children_as(self, node, scope, state, builder):
        """Walk ``node``'s children with ``scope`` (used where a declaration has no wrapper)."""
        stack = [(child, scope) for child in reversed(node.children)]
        while stack:
            current, current_scope = stack.pop()
            child_scope = self.visit(current, current_scope, state, builder)
            if child_scope is not None:
                stack.extend((c, child_scope) for c in reversed(current.children))
        return None

    # -- imports ----------------------------------------------------------------------------

    def _import(self, builder, module, site):
        target = builder.external("import", module, span_of(site))
        builder.relationship(RelationshipType.IMPORTS, builder.file_key, target, span_of(site),
                             certainty=Certainty.EXACT,
                             metadata={"module": module, "relative": module.startswith("."), "resolution": "unresolved"})

    @staticmethod
    def _bind_imports(node, scope, state):
        clause = next((c for c in node.named_children if c.type == "import_clause"), None)
        for identifier in (n for n in walk(clause) if n.type == "identifier") if clause is not None else []:
            if identifier.parent.type == "import_specifier" and identifier.parent.child_by_field_name("alias") \
                    is not None and identifier == identifier.parent.child_by_field_name("name"):
                continue  # `{ Router as R }` binds R
            state.bound[scope.namespace].add(text_of(identifier))

    # -- declarations -----------------------------------------------------------------------

    def _qualified(self, scope, name) -> str:
        return f"{scope.qualified_name}.{name}" if scope.qualified_name else name

    def _class(self, node, name, span_node, scope, state, builder):
        if node.type == "class_declaration" and _scala_header_broken(node):
            return None
        qualified = self._qualified(scope, name)
        key = builder.entity(EntityType.CLASS, name, span_of(span_node), parent_key=scope.key,
                             qualified_name=qualified, metadata={})
        state.classes.add(key)
        state.types[name].append(key)
        state.names[scope.namespace][name].append(_Declaration(key, span_of(node).start_line, True))
        heritage = next((c for c in node.named_children if c.type == "class_heritage"), None)
        base = next((c for c in heritage.named_children if c.type != "comment"), None) if heritage else None
        if base is not None and base.type in ("identifier", "member_expression") and _js_chain_is_simple(base):
            base_name = _name(text_of(base))
            lookup = "type" if base.type == "identifier" else "none"
            state.references.append(_Reference(RelationshipType.EXTENDS, key, span_of(base), "type", base_name,
                                               lookup=lookup, name=base_name, scope=scope))
            if lookup == "type":
                state.bases[key].append(base_name)
                state.superclass[key] = base_name
        # A class body is not a lexical scope for its members: they are reached through `this`.
        return _Scope(key, "class", qualified, key, None, scope.namespace, None)

    def _function(self, node, name, span_node, scope, state, builder, *, method, class_key=None,
                  arrow=False, metadata=None, owner=None):
        if node.type in ("function_declaration", "generator_function_declaration") and _scala_header_broken(node):
            return None
        parameters = node.child_by_field_name("parameters") or node.child_by_field_name("parameter")
        if parameters is not None and parameters.has_error:
            return None
        qualified = self._qualified(scope, name)
        key = builder.entity(EntityType.METHOD if method else EntityType.FUNCTION, name, span_of(span_node),
                             parent_key=scope.key, qualified_name=qualified, metadata=metadata or {})
        if method or owner:
            state.members[class_key or owner][name].append(key)
        else:
            state.names[scope.namespace][name].append(_Declaration(key, span_of(node).start_line, True))
        state.parent_namespace[key] = scope.namespace
        state.bound[key].update(_js_bound_names(parameters))
        for type_name, parameter in _JS_JSDOC_PARAM.findall(_js_jsdoc(span_node)):
            state.variables[key][parameter] = type_name
            state.bound[key].discard(parameter)
        # `this` is the class in methods and in arrow functions that inherit it; not in `function`s.
        this_class = class_key if method else (scope.class_key if arrow else None)
        return _Scope(key, "callable", qualified, this_class, key, key, None)

    def _declarator(self, node, scope, state, builder):
        name_node = node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        if name_node is None or name_node.type != "identifier":
            state.bound[scope.namespace].update(_js_bound_names(name_node))
            return scope
        name = text_of(name_node)
        declaration = node.parent
        single = declaration is not None and len([c for c in declaration.named_children
                                                  if c.type == "variable_declarator"]) == 1
        span_node = declaration if single else node
        if value is not None and value.type in _JS_FUNCTION_VALUES:
            child = self._function(value, name, span_node, scope, state, builder, method=False,
                                   arrow=value.type == "arrow_function")
            if child is not None:
                self._walk_children_as(value, child, state, builder)
            return None
        if value is not None and value.type in _JS_CLASS_VALUES:
            child = self._class(value, name, span_node, scope, state, builder)
            if child is not None:
                self._walk_children_as(value, child, state, builder)
            return None
        if value is not None and value.type == "call_expression" and self._require(value, builder):
            state.bound[scope.namespace].add(name)
            return None
        if value is not None and value.type == "object":
            self._object(value, name, scope, state, builder)
        declared = _JS_JSDOC_TYPE.search(_js_jsdoc(span_node))
        if declared:
            state.variables[scope.namespace][name] = declared.group(1)
        else:
            state.bound[scope.namespace].add(name)
        return scope

    def _object(self, value, name, scope, state, builder):
        """Functions in an object literal bound to a name: `const api = { get() {}, put: x => x }`."""
        holder = _Scope(scope.key, scope.kind, self._qualified(scope, name), scope.class_key, scope.callable_key,
                        scope.namespace, None)
        # Its functions are members of the object (reached as `name.member()`), not names in scope.
        owner = f"object:{holder.qualified_name}@{scope.namespace}"
        state.js_objects[(scope.namespace, name)] = owner
        for member in value.named_children:
            if member.type == "method_definition":
                key_node = member.child_by_field_name("name")
                function, span_node = member, member
            elif member.type == "pair" and (member.child_by_field_name("value") is not None
                                             and member.child_by_field_name("value").type in _JS_FUNCTION_VALUES):
                key_node, function, span_node = member.child_by_field_name("key"), \
                    member.child_by_field_name("value"), member
            else:
                continue
            child = self._function(function, _name(text_of(key_node)).strip("'\""), span_node, holder, state,
                                   builder, method=False, arrow=function.type == "arrow_function", owner=owner)
            if child is not None:
                self._walk_children_as(function, child, state, builder)
            state.handled.add(member.id)

    def _member(self, node, scope, state, builder):
        name_node = node.child_by_field_name("name") or node.child_by_field_name("property")
        name = _name(text_of(name_node))
        tokens = {c.type for c in node.children if not c.is_named}
        metadata = {k: True for k in ("static", "async") if k in tokens}
        for accessor in ("get", "set"):
            if accessor in tokens:
                metadata["accessor"] = accessor
        if node.type == "field_definition":
            value = node.child_by_field_name("value")
            if value is None or value.type not in _JS_FUNCTION_VALUES:
                return scope  # a data field: its initializer's calls belong to the class
            child = self._function(value, name, node, scope, state, builder, method=True, class_key=scope.class_key,
                                   arrow=value.type == "arrow_function", metadata=metadata)
            if child is not None:
                self._walk_children_as(value, child, state, builder)
            return None
        if name == "constructor":
            child = self._function(node, name, node, scope, state, builder, method=True, class_key=scope.class_key,
                                   metadata=metadata)
            if child is not None:
                state.constructors[scope.class_key].append(child.key)
            return child
        return self._function(node, name, node, scope, state, builder, method=True, class_key=scope.class_key,
                              metadata=metadata)

    def _commonjs_export(self, node, scope, state, builder) -> bool:
        """`exports.name = function ...` and `module.exports.name = ...` declare `name`."""
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        if left is None or right is None or left.type != "member_expression" or right.type not in _JS_FUNCTION_VALUES:
            return False
        target = _name(text_of(left.child_by_field_name("object")))
        if target not in ("exports", "module.exports"):
            return False
        name = _name(text_of(left.child_by_field_name("property")))
        statement = node.parent if node.parent is not None and node.parent.type == "expression_statement" else node
        child = self._function(right, name, statement, scope, state, builder, method=False,
                               arrow=right.type == "arrow_function")
        if child is not None:
            self._walk_children_as(right, child, state, builder)
        return True

    def _anonymous(self, node, scope, state):
        """A callback: not a declaration, so its calls belong to the enclosing function, but its
        parameters shadow outer names and a `function` rebinds `this`."""
        namespace = f"anonymous@{node.id}"
        state.parent_namespace[namespace] = scope.namespace
        parameters = node.child_by_field_name("parameters") or node.child_by_field_name("parameter")
        state.bound[namespace].update(_js_bound_names(parameters))
        this_class = scope.class_key if node.type == "arrow_function" else None
        return _Scope(scope.key, scope.kind, scope.qualified_name, this_class, scope.callable_key, namespace,
                      scope.body_id)

    # -- references -------------------------------------------------------------------------

    def _require(self, call, builder) -> bool:
        function = call.child_by_field_name("function")
        arguments = call.child_by_field_name("arguments")
        first = arguments.named_children[0] if arguments is not None and arguments.named_children else None
        if function is not None and (function.type == "import" or text_of(function) == "require"):
            module = _js_string(first)
            if module:
                self._import(builder, module, call)
                return True
        return False

    def _call(self, node, scope, state, builder):
        if self._require(node, builder):
            return
        function = node.child_by_field_name("function")
        source = scope.callable_key or scope.key
        span = span_of(node)
        arguments = node.child_by_field_name("arguments")
        count = len([c for c in arguments.named_children if c.type != "comment"]) if arguments is not None else None
        if function is None:
            return
        if function.type == "identifier":
            name = text_of(function)
            state.references.append(_Reference(RelationshipType.CALLS, source, span, "call", name, lookup="lexical",
                                               name=name, scope=scope, arguments=count))
        elif function.type == "super":
            state.references.append(_Reference(RelationshipType.CALLS, source, span, "call", "super",
                                               lookup="super_constructor", name="super", scope=scope,
                                               arguments=count))
        elif function.type == "member_expression":
            receiver = function.child_by_field_name("object")
            name = _name(text_of(function.child_by_field_name("property")))
            simple = _js_chain_is_simple(receiver)
            display = f"{_name(text_of(receiver)) if simple else EXPRESSION}.{name}"
            receiver_name, lookup = None, "none"
            if receiver.type == "this":
                lookup = "this"
            elif receiver.type == "super":
                lookup = "super"
            elif receiver.type == "identifier":
                lookup, receiver_name = "receiver", text_of(receiver)
            elif receiver.type == "new_expression":
                constructor = receiver.child_by_field_name("constructor")
                if constructor is not None and constructor.type == "identifier":
                    lookup, receiver_name = "instance", text_of(constructor)
            state.references.append(_Reference(RelationshipType.CALLS, source, span, "call", display, lookup=lookup,
                                               name=name, scope=scope, arguments=count, receiver=receiver_name))

    def _new(self, node, scope, state):
        constructor = node.child_by_field_name("constructor")
        if constructor is None or not _js_chain_is_simple(constructor):
            return
        name = _name(text_of(constructor))
        arguments = node.child_by_field_name("arguments")
        state.references.append(_Reference(
            RelationshipType.CALLS, scope.callable_key or scope.key, span_of(node), "call", name,
            lookup="new" if constructor.type == "identifier" else "none", name=name, scope=scope,
            arguments=len(arguments.named_children) if arguments is not None else 0))

    # -- resolution -------------------------------------------------------------------------

    def candidates(self, ref, state):
        scope = ref.scope
        if ref.lookup == "type":
            return list(state.types.get(ref.name, []))
        if ref.lookup == "lexical":
            found = self._lexical(state, scope.namespace, ref.name)
            return [k for k in found if k not in state.classes]  # calling a class without `new` is an error
        if ref.lookup == "this":
            return self._members(state, scope.class_key, ref.name) if scope.class_key else []
        if ref.lookup == "super":
            return self._members(state, scope.class_key, ref.name, bases_only=True) if scope.class_key else []
        if ref.lookup == "super_constructor":
            base = state.superclass.get(scope.class_key)
            return self._constructor(state, base) if base else []
        if ref.lookup == "new":
            classes = [k for k in self._lexical(state, scope.namespace, ref.name) if k in state.classes]
            return self._constructor(state, classes[0]) if len(classes) == 1 else []
        if ref.lookup == "instance":
            classes = [k for k in self._lexical(state, scope.namespace, ref.receiver) if k in state.classes]
            return self._members(state, classes[0], ref.name) if len(classes) == 1 else []
        if ref.lookup == "receiver":
            owner = self._object_owner(state, scope.namespace, ref.receiver)
            if owner is not None:  # helpers.summarize() on an object literal bound in scope
                state.basis_override[id(ref)] = "lexical"
                return list(state.members[owner].get(ref.name, []))
            type_name, shadowed = self._declared(state, scope.namespace, ref.receiver)
            if type_name:
                keys = state.types.get(type_name, [])
                return self._members(state, keys[0], ref.name) if len(keys) == 1 else []
            if not shadowed:
                classes = [k for k in self._lexical(state, scope.namespace, ref.receiver) if k in state.classes]
                if len(classes) == 1:  # Class.staticMethod()
                    state.basis_override[id(ref)] = "type"
                    return self._members(state, classes[0], ref.name)
            return []
        return []

    @staticmethod
    def _constructor(state, class_key) -> list[str]:
        constructors = state.constructors.get(class_key, [])
        return constructors[:1] if constructors else [class_key]

    @staticmethod
    def _lexical(state, namespace, name) -> list[str]:
        while namespace is not None:
            declarations = state.names[namespace].get(name)
            if declarations:
                return [d.key for d in declarations]
            if name in state.bound[namespace] or name in state.variables.get(namespace, {}):
                return []
            namespace = state.parent_namespace.get(namespace)
        return []

    @staticmethod
    def _declared(state, namespace, name) -> tuple[str | None, bool]:
        while namespace is not None:
            if name in state.variables.get(namespace, {}):
                return state.variables[namespace][name], False
            if state.names[namespace].get(name):
                return None, False  # a declared function or class: it has no declared variable type
            if name in state.bound[namespace]:
                return None, True
            namespace = state.parent_namespace.get(namespace)
        return None, False

    @staticmethod
    def _object_owner(state, namespace, name) -> str | None:
        """The object literal ``name`` is bound to in the nearest scope that binds it, if any."""
        while namespace is not None:
            if (namespace, name) in state.js_objects:
                return state.js_objects[(namespace, name)]
            if state.names[namespace].get(name) or name in state.variables.get(namespace, {}) \
                    or name in state.bound[namespace]:
                return None
            namespace = state.parent_namespace.get(namespace)
        return None

    def prepare(self, state) -> None:
        for class_key, names in list(state.bases.items()):
            state.bases[class_key] = [k for n in names for k in state.types.get(n, [])[:1]
                                      if len(state.types.get(n, [])) == 1]
        for class_key, name in list(state.superclass.items()):
            keys = state.types.get(name, [])
            if len(keys) == 1:
                state.superclass[class_key] = keys[0]
            else:
                del state.superclass[class_key]


# --- Ruby ------------------------------------------------------------------------------------

_RUBY_IMPORTS = frozenset({"require", "require_relative", "load"})
_RUBY_MIXINS = {"include": "instance", "prepend": "instance", "extend": "singleton"}
# Class-body declarations written as method calls; they are structure, not calls.
_RUBY_MACROS = frozenset({
    "private", "protected", "public", "module_function", "private_constant", "attr_reader", "attr_writer",
    "attr_accessor", "include", "extend", "prepend", "require", "require_relative", "load",
})
# Parents in which a bare identifier is an expression, so a name that is not a local variable is a
# call to a method on self (Ruby decides the same way).
_RUBY_EXPRESSION_PARENTS = frozenset({
    "binary", "unary", "argument_list", "body_statement", "then", "else", "begin", "return",
    "parenthesized_statements", "conditional", "array", "pair", "interpolation", "element_reference",
    "if", "unless", "while", "until", "if_modifier", "unless_modifier", "while_modifier", "until_modifier",
    "method", "singleton_method", "block_body", "do_block", "block", "program", "range", "splat_argument",
})
_RUBY_YARD_PARAM = re.compile(r"@param\s+(?:\[([\w:]+)\]\s+(\w+)|(\w+)\s+\[([\w:]+)\])")


def _ruby_constant(node: Any) -> str | None:
    """`Foo` or `A::B` as a dotted name; None for any other expression."""
    if node is None:
        return None
    if node.type == "constant":
        return text_of(node)
    if node.type == "scope_resolution":
        scope, name = node.child_by_field_name("scope"), node.child_by_field_name("name")
        prefix = _ruby_constant(scope) if scope is not None else ""
        return f"{prefix}.{text_of(name)}" if prefix else text_of(name)
    return None


def _ruby_string(node: Any) -> str | None:
    if node is None or node.type != "string":
        return None
    if any(c.type == "interpolation" for c in node.named_children):
        return None
    return "".join(text_of(c) for c in node.named_children if c.type == "string_content")


def _ruby_chain_is_simple(node: Any) -> bool:
    while node.type == "call" and node.child_by_field_name("arguments") is None \
            and node.child_by_field_name("block") is None and node.child_by_field_name("receiver") is not None:
        node = node.child_by_field_name("receiver")
    return node.type in ("identifier", "constant", "scope_resolution", "self", "instance_variable",
                         "class_variable", "global_variable")


def _ruby_parameter_names(parameters: Any) -> list[str]:
    names = []
    for parameter in parameters.named_children if parameters is not None else []:
        if parameter.type == "identifier":
            names.append(text_of(parameter))
        else:
            name = parameter.child_by_field_name("name")
            if name is not None:
                names.append(text_of(name))
            elif parameter.type in ("destructured_parameter", "block_parameters"):
                names.extend(_ruby_parameter_names(parameter))
    return names


def _ruby_yard(node: Any) -> dict[str, str]:
    """`@param name [Type]` (or `@param [Type] name`) from the comments directly above a method."""
    types, previous = {}, node.prev_sibling
    line = node.start_point[0]
    while previous is not None and previous.type == "comment" and previous.end_point[0] + 1 >= line:
        for match in _RUBY_YARD_PARAM.finditer(text_of(previous)):
            type_name, name = (match.group(1), match.group(2)) if match.group(1) else (match.group(4), match.group(3))
            types[name] = type_name.split("::")[-1]
        line = previous.start_point[0]
        previous = previous.prev_sibling
    return types


class RubyAnalyzer(TreeSitterAnalyzer):
    """Ruby classes, modules and methods. Modules are INTERFACEs (mixins); `include`/`prepend`/
    `extend` are IMPLEMENTS. Singleton methods are `<Class>.self.<name>`. Metaprogramming
    (`define_method`, `attr_*`, `method_missing`, `send`) declares nothing the analyzer reports."""

    language = "ruby"
    extractor = "tree-sitter-ruby"
    analyzer_version = "stacksniffer-ruby/1"
    capabilities = AnalyzerCapabilities({
        Capability.DECLARATIONS: S,
        Capability.IMPORTS: S,
        Capability.CALLS: P,
        Capability.INHERITANCE: P,
        Capability.INTERFACES: P,  # modules mixed in
        Capability.DEPENDENCIES: U,  # Gemfile is analyzed as Ruby code, not as dependencies
        Capability.DECORATORS: U,
    })

    def __init__(self, adapter: TreeSitterParserAdapter | None = None):
        super().__init__(adapter or TreeSitterParserAdapter("ruby", "tree_sitter_ruby", "tree-sitter-ruby"))

    def visit(self, node, scope, state, builder):
        kind = node.type
        if kind == "ERROR" and node.parent is not None:
            return None
        if kind in ("class", "module"):
            return self._namespace(node, scope, state, builder)
        if kind == "singleton_class":  # class << self
            return _Scope(scope.key, "class", scope.qualified_name, scope.class_key, None,
                          f"{scope.class_key}|self" if scope.class_key else scope.namespace, None) \
                if scope.class_key else scope
        if kind in ("method", "singleton_method"):
            return self._method(node, scope, state, builder)
        if kind in ("assignment", "operator_assignment"):
            left = node.child_by_field_name("left")
            targets = [left] if left is not None and left.type == "identifier" else \
                [n for n in walk(left) if n.type == "identifier"] if left is not None and \
                left.type in ("left_assignment_list", "destructured_left_assignment") else []
            state.bound[self._locals(scope)].update(text_of(t) for t in targets)
        elif kind in ("block_parameters", "lambda_parameters"):
            state.bound[self._locals(scope)].update(_ruby_parameter_names(node))
            return None
        elif kind == "exception_variable":  # rescue Error => e
            state.bound[self._locals(scope)].update(text_of(n) for n in walk(node) if n.type == "identifier")
            return None
        elif kind == "call":
            return self._call(node, scope, state, builder)
        elif kind == "identifier":
            self._bare(node, scope, state)
        elif kind == "super":
            self._super(node, scope, state)
        return scope

    @staticmethod
    def _locals(scope) -> str:
        return scope.callable_key or scope.namespace

    # -- declarations -----------------------------------------------------------------------

    def _namespace(self, node, scope, state, builder):
        if _scala_header_broken(node):
            return None
        name = _ruby_constant(node.child_by_field_name("name"))
        if not name:
            return None
        module = node.type == "module"
        qualified = f"{scope.qualified_name}.{name}" if scope.qualified_name else name
        key = builder.entity(EntityType.INTERFACE if module else EntityType.CLASS, name.rsplit(".", 1)[-1],
                             span_of(node), parent_key=scope.key, qualified_name=qualified,
                             metadata={"kind": node.type})
        state.types[name.rsplit(".", 1)[-1]].append(key)
        state.classes.add(key)
        state.parent_namespace[key] = scope.namespace
        state.parent_namespace[f"{key}|self"] = scope.namespace
        if module:
            state.traits.add(key)
        superclass = node.child_by_field_name("superclass")
        if superclass is not None:
            base = next((c for c in superclass.named_children), None)
            base_name = _ruby_constant(base)
            if base_name:
                simple = "." not in base_name
                state.references.append(_Reference(RelationshipType.EXTENDS, key, span_of(base), "type",
                                                   base_name.replace(".", "::"), lookup="type" if simple else "none",
                                                   name=base_name, scope=scope))
                if simple:
                    state.bases[key].append(base_name)
                    state.bases[f"{key}|self"].append(f"{base_name}|self")
                    state.superclass[key] = base_name
        return _Scope(key, "class", qualified, key, None, key, None)

    def _method(self, node, scope, state, builder):
        if _scala_header_broken(node):
            return None
        name = text_of(node.child_by_field_name("name"))
        singleton = node.type == "singleton_method" or (scope.namespace or "").endswith("|self")
        owner = scope.class_key
        if node.type == "singleton_method":
            target = node.child_by_field_name("object")
            if target is None or target.type != "self":
                owner = None  # def obj.method: a singleton of some other object
        if owner:
            qualified = f"{scope.qualified_name}.{'self.' if singleton else ''}{name}"
            entity_type = EntityType.METHOD
        else:
            qualified = f"{scope.qualified_name}.{name}" if scope.qualified_name else name
            entity_type = EntityType.FUNCTION
        key = builder.entity(entity_type, name, span_of(node), parent_key=scope.key, qualified_name=qualified,
                             metadata={"singleton": True} if singleton and owner else {})
        member_table = f"{owner}|self" if singleton and owner else owner
        if owner:
            state.members[member_table][name].append(key)
            if name == "initialize" and not singleton:
                state.constructors[owner].append(key)
        else:
            state.names[scope.namespace][name].append(_Declaration(key, span_of(node).start_line, True))
        parameters = node.child_by_field_name("parameters")
        state.bound[key].update(_ruby_parameter_names(parameters))
        for parameter, type_name in _ruby_yard(node).items():
            state.variables[key][parameter] = type_name
        state.parent_namespace[key] = scope.namespace
        state.ruby_methods = getattr(state, "ruby_methods", {})
        state.ruby_methods[key] = (name, member_table if owner else None, owner)
        return _Scope(key, "callable", qualified, member_table if owner else None, key, key, None)

    # -- references -------------------------------------------------------------------------

    def _call(self, node, scope, state, builder):
        receiver = node.child_by_field_name("receiver")
        method = node.child_by_field_name("method")
        if method is None:
            return scope
        if method.type == "super":  # super(...) with arguments
            self._super(node, scope, state)
            return scope
        name = _name(text_of(method))
        arguments = node.child_by_field_name("arguments")
        span = span_of(node)
        source = scope.callable_key or scope.key
        if receiver is None:
            if name in _RUBY_IMPORTS:
                module = _ruby_string(arguments.named_children[0]) if arguments is not None and \
                    arguments.named_children else None
                if module:
                    target = builder.external("import", module, span)
                    builder.relationship(RelationshipType.IMPORTS, builder.file_key, target, span,
                                         certainty=Certainty.EXACT,
                                         metadata={"module": module, "relative": name == "require_relative",
                                                   "resolution": "unresolved"})
                return None
            if name in _RUBY_MIXINS and scope.kind == "class" and scope.class_key:
                for argument in arguments.named_children if arguments is not None else []:
                    module = _ruby_constant(argument)
                    if module:
                        simple = "." not in module
                        state.references.append(_Reference(
                            RelationshipType.IMPLEMENTS, scope.class_key, span_of(argument), "type",
                            module.replace(".", "::"), lookup="type" if simple else "none", name=module, scope=scope,
                            metadata={"mixin": name}))
                        if simple:
                            table = scope.class_key if _RUBY_MIXINS[name] == "instance" else f"{scope.class_key}|self"
                            state.bases[table].append(module)
                return None
            if name in _RUBY_MACROS:
                return None
            state.references.append(_Reference(RelationshipType.CALLS, source, span, "call", name,
                                               lookup="unqualified", name=name, scope=scope))
            return scope
        display = f"{_name(text_of(receiver)).replace('&.', '.') if _ruby_chain_is_simple(receiver) else EXPRESSION}" \
                  f".{name}"
        lookup, receiver_name = "none", None
        if receiver.type == "self":
            lookup = "this"
        elif receiver.type in ("constant", "scope_resolution"):
            constant = _ruby_constant(receiver)
            lookup, receiver_name = ("new" if name == "new" else "type"), constant
        elif receiver.type == "identifier":
            lookup, receiver_name = "receiver", text_of(receiver)
        state.references.append(_Reference(RelationshipType.CALLS, source, span, "call", display, lookup=lookup,
                                           name=name, scope=scope, receiver=receiver_name))
        return scope

    def _bare(self, node, scope, state):
        """`subtotal` alone is a call to self.subtotal unless a local variable of that name exists."""
        parent = node.parent
        if parent is None or scope.callable_key is None:
            return
        if parent.type == "call":
            if parent.child_by_field_name("receiver") != node:
                return  # the called method's own name
        elif parent.type in ("assignment", "operator_assignment"):
            if parent.child_by_field_name("right") != node:
                return
        elif parent.type not in _RUBY_EXPRESSION_PARENTS:
            return
        if parent.type in ("method", "singleton_method") and parent.child_by_field_name("body") != node:
            return  # the method's name
        if parent.type == "pair" and parent.child_by_field_name("value") != node:
            return
        name = text_of(node)
        if name in _RUBY_MACROS or self._is_local(state, scope, name):
            return
        state.references.append(_Reference(RelationshipType.CALLS, scope.callable_key, span_of(node), "call", name,
                                           lookup="unqualified", name=name, scope=scope))

    def _super(self, node, scope, state):
        methods = getattr(state, "ruby_methods", {})
        if scope.callable_key not in methods:
            return
        name, table, owner = methods[scope.callable_key]
        constructor = name == "initialize" and table == owner
        state.references.append(_Reference(RelationshipType.CALLS, scope.callable_key, span_of(node), "call",
                                           "super", lookup="super_constructor" if constructor else "super",
                                           name=name, scope=scope))

    @staticmethod
    def _is_local(state, scope, name) -> bool:
        namespace = scope.callable_key
        return name in state.bound[namespace] or name in state.variables.get(namespace, {})

    # -- resolution -------------------------------------------------------------------------

    def candidates(self, ref, state):
        scope = ref.scope
        if ref.lookup == "type" and ref.relationship_type is not RelationshipType.CALLS:
            return list(state.types.get(ref.name, []))
        if ref.lookup == "unqualified":
            if ref.name == "new" and scope.class_key and scope.class_key.endswith("|self"):
                state.basis_override[id(ref)] = "new"  # `new` inside a class method
                return self._constructor(state, scope.class_key[:-5])
            if scope.class_key:
                found = self._members(state, scope.class_key, ref.name)
                if found:
                    return found
            found = []
            namespace = scope.namespace
            while namespace is not None and not found:
                found = [d.key for d in state.names[namespace].get(ref.name, [])]
                namespace = state.parent_namespace.get(namespace)
            if found:
                state.basis_override[id(ref)] = "lexical"  # a top-level method
            return found
        if ref.lookup == "this":
            return self._members(state, scope.class_key, ref.name) if scope.class_key else []
        if ref.lookup == "super":
            return self._members(state, scope.class_key, ref.name, bases_only=True) if scope.class_key else []
        if ref.lookup == "super_constructor":
            base = state.superclass.get(scope.class_key)
            return self._constructor(state, base) if base else []
        if ref.lookup in ("new", "type") and ref.relationship_type is RelationshipType.CALLS:
            classes = [k for k in state.types.get((ref.receiver or "").rsplit(".", 1)[-1], [])]
            if len(classes) != 1:
                return []
            if ref.lookup == "new":
                return self._constructor(state, classes[0]) if classes[0] not in state.traits else []
            return self._members(state, f"{classes[0]}|self", ref.name)
        if ref.lookup == "receiver":
            type_name = state.variables.get(scope.callable_key, {}).get(ref.receiver)
            keys = state.types.get(type_name, []) if type_name else []
            return self._members(state, keys[0], ref.name) if len(keys) == 1 else []
        return []

    @staticmethod
    def _constructor(state, class_key) -> list[str]:
        constructors = state.constructors.get(class_key, [])
        return constructors[:1] if constructors else [class_key]

    def prepare(self, state) -> None:
        def resolve(name):
            singleton = name.endswith("|self")
            keys = state.types.get(name[:-5] if singleton else name, [])
            if len(keys) != 1:
                return None
            return f"{keys[0]}|self" if singleton else keys[0]

        for table, names in list(state.bases.items()):
            state.bases[table] = [key for key in map(resolve, names) if key]
        for class_key, name in list(state.superclass.items()):
            key = resolve(name)
            if key is not None:
                state.superclass[class_key] = key
            else:
                del state.superclass[class_key]
