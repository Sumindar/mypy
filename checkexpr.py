from mtypes import (
    Typ, Any, Callable, Overloaded, NoneTyp, Void, TypeVarDef, TypeVars,
    TupleType, Instance, TypeVar
)
from nodes import (
        NameExpr, RefExpr, Var, FuncDef, OverloadedFuncDef, TypeInfo, CallExpr,
        Node, MemberExpr, IntExpr, StrExpr, FloatExpr, OpExpr, UnaryExpr,
        IndexExpr, CastExpr, TypeApplication, ListExpr, TupleExpr, DictExpr,
        FuncExpr, SuperExpr, ParenExpr, SliceExpr, Context
)
from nodes import function_type, method_type
import checker
from sametypes import is_same_type
from replacetvars import replace_func_type_vars, replace_type_vars
from messages import MessageBuilder
import messages
from infer import infer_type_arguments, infer_function_type_arguments
from expandtype import expand_type, expand_caller_var_args
from subtypes import is_subtype
import erasetype
from checkmember import analyse_member_access
from semanal import self_type


# This class type checks expressions. It works closely together with
# TypeChecker.
class ExpressionChecker:
    # Some services are provided by a TypeChecker instance.
    checker.TypeChecker chk
    # This is shared with TypeChecker, but stored also here for convenience.
    MessageBuilder msg    
    
    # Construct an expression checker.
    void __init__(self,
                  checker.TypeChecker chk,
                  MessageBuilder msg):
        self.chk = chk
        self.msg = msg
    
    # Type check a name expression (of any kind: local, member or global).
    Typ visit_name_expr(self, NameExpr e):
        return self.analyse_ref_expr(e)
    
    Typ analyse_ref_expr(self, RefExpr e):
        Typ result
        node = e.node
        if isinstance(node, Var):
            # Variable or constant reference.
            v = (Var)node
            if v.typ is None:
                # Implicit dynamic type.
                result = Any()
            else:
                # Local or global variable.
                result = v.typ.typ
        elif isinstance(node, FuncDef):
            # Reference to a global function.
            f = (FuncDef)node
            result = function_type(f)
        elif isinstance(node, OverloadedFuncDef):
            o = (OverloadedFuncDef)node
            result = o.typ.typ
        elif isinstance(node, TypeInfo):
            # Reference to a type object.
            result = self.type_object_type((TypeInfo)node)
        else:
            # Unknown reference; use dynamic type implicitly to avoid generating
            # extra type errors.
            result = Any()
        return result
    
    # Analyse direct member access via a name expression (implicit self). This
    # can access private definitions.
    Typ analyse_direct_member_access(self, str name, TypeInfo info, bool is_lvalue, Context context):
        raise RuntimeError('Not implemented')
    
    # Type check a call expression.
    Typ visit_call_expr(self, CallExpr e):
        self.accept(e.callee)
        # Access callee type directly, since accept may return the any type even
        # if the type is known (in a dynamically typed function). This way we get
        #  a more precise callee in dynamically typed functions.
        callee_type = self.chk.type_map[e.callee]
        self.chk.store_type(e.callee, callee_type)
        return self.check_call_expr_with_callee_type(callee_type, e)
    
    # Type check call expression. The given callee type overrides the type of
    # the callee expression.
    Typ check_call_expr_with_callee_type(self, Typ callee_type, CallExpr e):
        
        return self.check_call(callee_type, e.args, e, e.is_var_arg)
    
    # Type check a call with the given callee and argument types. If isVarArg
    # is True, the callee uses varargs. If checkArgCount is False, do not report
    # invalid number of arguments as an error (this is used when the error has
    # already been reported by the semantic analyzer and we don't want duplicate
    # error messages).
    Typ check_call(self, Typ callee, list<Node> args, Context context, bool is_var_arg=False, bool check_arg_count=True):
        if isinstance(callee, Callable):
            callable = (Callable)callee
            list<Typ> arg_types
            
            if callable.is_generic():
                callable = self.infer_function_type_arguments_using_context(callable)
                arg_types = self.infer_arg_types_in_context(callable, args)
                callable = self.infer_function_type_arguments(callable, arg_types, is_var_arg, context)
            
            arg_types = self.infer_arg_types_in_context(callable, args)
            
            # Check number of arguments, but only if the semantic analyzer hasn't
            # done it for us.
            if check_arg_count:
                # Checking the type and compatibility of the varargs argument type in
                # a call is handled by the checkArgumentTypes call below.
                if not is_valid_argc(len(args), is_var_arg, callable):
                    self.msg.invalid_argument_count(callable, len(args), context)
            
            self.check_argument_types(arg_types, is_var_arg, callable, context)
            
            return callable.ret_type
        elif isinstance(callee, Overloaded):
            # Type check arguments in empty context. They will be checked again
            # later in a context derived from the signature; these types are only
            # used to pick a signature variant.
            arg_types = self.infer_arg_types_in_context(None, args)
            
            target = self.overload_call_target(arg_types, is_var_arg, (Overloaded)callee, context)
            return self.check_call(target, args, context, is_var_arg, check_arg_count)
        elif isinstance(callee, Any) or self.chk.is_dynamic_function():
            self.infer_arg_types_in_context(None, args)
            return Any()
        else:
            return self.msg.not_callable(callee, context)
    
    list<Typ> infer_arg_types_in_context(self, Callable callee, list<Node> args):
        list<Typ> res = []
        
        fixed = len(args)
        if callee is not None:
            fixed = min(fixed, callee.max_fixed_args())
        
        for i in range(fixed):
            arg = args[i]#FIX refactor
            Typ ctx = None
            if callee is not None and i < len(callee.arg_types):
                ctx = callee.arg_types[i]
            res.append(self.accept(arg, ctx))
        
        for j in range(fixed, len(args)):
            if callee is not None and callee.is_var_arg:
                res.append(self.accept(args[j], callee.arg_types[-1]))
            else:
                res.append(self.accept(args[j]))
        
        return res
    
    Callable infer_function_type_arguments_using_context(self, Callable callable):
        ctx = self.chk.type_context[-1]
        if ctx is None:
            return callable
        # The return type may have references to function type variables that we
        # are inferring right now. We must consider them as indeterminate and they
        # are not potential results; thus we replace them with the nil type. On
        # the other hand, class type variables are valid results.
        erased_ctx = replace_func_type_vars(ctx)
        args = infer_type_arguments(callable.type_var_ids(), callable.ret_type, erased_ctx, self.chk.basic_types())
        # If all the inferred types are nil types, do no type variable substution.
        # TODO This is not nearly general enough. If a type has a nil type
        #      component we should not use it. Also if some types are not-nil we
        #      should only substitute them. Finally, using nil types for this
        #      might not be optimal.
        some_not_nil = False
        for i in range(len(args)):
            if not isinstance(args[i], NoneTyp):
                some_not_nil = True
        if not some_not_nil:
            return callable
        return (Callable)self.apply_generic_arguments(callable, args, [], None)
    
    # Infer the type arguments for a generic callee type. Return a derived
    # callable type that has the arguments applied (and stored as implicit
    # type arguments). If isVarArg is True, the callee uses varargs.
    Callable infer_function_type_arguments(self, Callable callee_type, list<Typ> arg_types, bool is_var_arg, Context context):
        list<Typ> inferred_args = infer_function_type_arguments(callee_type, arg_types, is_var_arg, self.chk.basic_types())
        return self.apply_inferred_arguments(callee_type, inferred_args, [], context)
    
    # Apply inferred values of type arguments to a generic function. If
    # implicitTypeVars are given, they correspond to the ids of the implicit
    # instance type variables; they are stored as the prefix of inferredArgs.
    # InferredArgs contains first the values of implicit instance type vars (if
    # any), and then values of function type variables, concatenated together.
    Callable apply_inferred_arguments(self, Callable callee_type, list<Typ> inferred_args, list<int> implicit_type_vars, Context context):
        # Report error if some of the variables could not be solved. In that case
        # assume that all variables have type dynamic to avoid extra bogus error
        # messages.
        for i in range(len(inferred_args)):
            inferred_type = inferred_args[i]
            if inferred_type is None:
                # Could not infer a non-trivial type for a type variable.
                self.msg.could_not_infer_type_arguments(callee_type, i + 1 - len(implicit_type_vars), context)
                inferred_args = <Typ> [Any()] * len(inferred_args)
        
        # Apply the inferred types to the function type. In this case the return
        # type must be Callable, since we give the right number of type arguments.
        return (Callable)self.apply_generic_arguments(callee_type, inferred_args, implicit_type_vars, None)
    
    # Check argument types against a callable type. If isVarArg is True,
    # the caller uses varargs.
    void check_argument_types(self, list<Typ> arg_types, bool is_var_arg, Callable callee, Context context):
        callee_num_args = callee.max_fixed_args()
        
        Typ caller_rest = None # Rest of types for varargs calls
        if is_var_arg:
            # Varargs call site.
            
            if not self.is_valid_var_arg(arg_types[-1]):
                self.msg.invalid_var_arg(arg_types[-1], context)
                return 
            
            arg_types, caller_rest = expand_caller_var_args(arg_types, callee_num_args)
            
            # Check vararg call argument count.
            if len(arg_types) < callee.min_args:
                self.msg.too_few_arguments(callee, context)
            elif len(arg_types) > len(callee.arg_types) and not callee.is_var_arg:
                self.msg.too_many_arguments(callee, context)
            elif caller_rest is not None and not callee.is_var_arg and not isinstance(arg_types[-1], Any):
                self.msg.too_many_arguments(callee, context)
            
            # Check vararg types.
            if caller_rest is not None and callee.is_var_arg:
                self.chk.check_subtype(caller_rest, callee.arg_types[-1], context, messages.INCOMPATIBLE_ARRAY_VAR_ARGS)
        
        caller_num_args = len(arg_types)
        
        # Verify fixed argument types.
        for i in range(min(caller_num_args, callee_num_args)):
            self.check_arg(arg_types[i], callee.arg_types[i], i + 1, callee, context)
        
        # Verify varargs.
        if callee.is_var_arg:
            for j in range(callee_num_args, caller_num_args):
                self.check_arg(arg_types[j], callee.arg_types[-1], j + 1,
                               callee, context)
    
    # Check the type of a single argument in a call.
    void check_arg(self, Typ caller_type, Typ callee_type, int n, Callable callee, Context context):
        if isinstance(caller_type, Void):
            self.msg.does_not_return_value(caller_type, context)
        elif not is_subtype(caller_type, callee_type):
            self.msg.incompatible_argument(n, callee, caller_type, context)
    
    # Infer the correct overload item to call with the given argument
    # types. The return value may be Callable or any (if an unique item
    # could not be determined). If isVarArg is True, the caller uses varargs.
    #
    # TODO for overlapping signatures we should try to get a more precise result
    #      than 'any'
    Typ overload_call_target(self, list<Typ> arg_types, bool is_var_arg, Overloaded overload, Context context):
        Typ match = None # Callable, Dynamic or nil
        for typ in overload.items():
            if self.matches_signature(arg_types, is_var_arg, typ):
                if match is not None and (isinstance(match, Any) or not is_same_type(((Callable)match).ret_type, typ.ret_type)):
                    # Ambiguous return type. Either the function overload is overlapping
                    # (which results in an error elsewhere) or the caller has provided
                    # some dynamic argument types; in either case can only infer the
                    # type to be any, as it is not an error to use any types in calls.
                    # TODO overlapping overloads should be possible in some cases
                    match = Any()
                else:
                    match = typ
        if match is None:
            self.msg.no_variant_matches_arguments(overload, context)
            return Any()
        else:
            return match
    
    # Determine whether argument types match the given signature. If isVarArg
    # is True, the caller uses varargs.
    bool matches_signature(self, list<Typ> arg_types, bool is_var_arg, Callable typ):
        if not is_valid_argc(len(arg_types), False, typ):
            return False
        
        if is_var_arg:
            if not self.is_valid_var_arg(arg_types[-1]):
                return False
            Typ rest
            arg_types, rest = expand_caller_var_args(arg_types, typ.max_fixed_args())
        
        for i in range(len(arg_types)):
            if not is_subtype(erasetype.erase_type(arg_types[i],
                                                   self.chk.basic_types()),
                              erasetype.erase_type(
                                  replace_type_vars(typ.arg_types[i]),
                                  self.chk.basic_types())):
                return False
        return True
    
    # Apply generic type arguments to a callable type. For example, applying
    # Int to 'def <T> (T) -> T' results in 'def [Int] (Int) -> Int'. Here
    # '[Int]' is an implicit bound type variable.
    #
    # Note that each type can be nil; in this case, it will not be applied.
    Typ apply_generic_arguments(self, Callable callable, list<Typ> types, list<int> implicit_type_vars, Context context):
        list<TypeVarDef> tvars = []
        for v in implicit_type_vars:
            # The name of type variable is not significant, so nil is fine.
            tvars.append(TypeVarDef(None, v))
        tvars.extend(callable.variables.items)
        
        if len(tvars) != len(types):
            self.msg.incompatible_type_application(len(tvars), len(types), context)
            return Any()
        
        # Create a map from type variable name to target type.
        dict<int, Typ> map = {}
        for i in range(len(tvars)):
            if types[i] is not None:
                map[tvars[i].id] = types[i]
        
        list<Typ> arg_types = []
        for at in callable.arg_types:
            arg_types.append(expand_type(at, map))
        
        list<tuple<int, Typ>> bound_vars = []
        for tv in tvars:
            if tv.id in map:
                bound_vars.append((tv.id, map[tv.id]))
        
        return Callable(arg_types,
                        callable.min_args,
                        callable.is_var_arg,
                        expand_type(callable.ret_type, map),
                        callable.is_type_obj(),
                        callable.name,
                        TypeVars([]),
                        callable.bound_vars + bound_vars,
                        callable.line, callable.repr)
    
    # Visit member expression (of form e.id).
    Typ visit_member_expr(self, MemberExpr e):
        return self.analyse_ordinary_member_access(e, False)
    
    # Analyse member expression or member lvalue.
    Typ analyse_ordinary_member_access(self, MemberExpr e, bool is_lvalue):
        if e.kind is not None:
            # This is a reference to a module attribute.
            return self.analyse_ref_expr(e)
        else:
            # This is a reference to a non-module attribute.
            return analyse_member_access(e.name, self.accept(e.expr), e, is_lvalue, False, self.chk.tuple_type(), self.msg)
    
    # Analyse member access that is external, i.e. it cannot refer to private
    # definitions. Return the result type.
    Typ analyse_external_member_access(self, str member, Typ base_type, Context context):
        return analyse_member_access(member, base_type, context, False, False, self.chk.tuple_type(), self.msg)
    
    # Type check an integer literal (trivial).
    Typ visit_int_expr(self, IntExpr e):
        return self.named_type('builtins.int')
    
    # Type check a string literal (trivial).
    Typ visit_str_expr(self, StrExpr e):
        return self.named_type('builtins.str')
    
    # Type check a float literal (trivial).
    Typ visit_float_expr(self, FloatExpr e):
        return self.named_type('builtins.float')
    
    # Visit a binary operator expression.
    Typ visit_op_expr(self, OpExpr e):
        left_type = self.accept(e.left)
        right_type = self.accept(e.right) # TODO only evaluate if needed
        if e.op == 'in' or e.op == 'not in':
            result = self.check_op('__contains__', right_type, e.left, e)
            if e.op == 'in':
                return result
            else:
                return self.chk.bool_type()
        elif e.op in checker.op_methods:
            method = checker.op_methods[e.op]
            return self.check_op(method, left_type, e.right, e)
        elif e.op == 'and' or e.op == 'or':
            return self.check_boolean_op(e.op, left_type, right_type, e)
        elif e.op == 'is' or e.op == 'is not':
            return self.chk.bool_type()
        else:
            raise RuntimeError('Unknown operator {}'.format(e.op))
    
    # Type check a binary operation which maps to a method call.
    Typ check_op(self, str method, Typ base_type, Node arg, Context context):
        if self.has_non_method(base_type, method):
            self.msg.method_expected_as_operator_implementation(base_type, method, context)
        method_type = self.analyse_external_member_access(method, base_type, context)
        return self.check_call(method_type, [arg], context, False, True)
    
    # Type check a boolean operation ("and" or "or").
    Typ check_boolean_op(self, str op, Typ left_type, Typ right_type, Context context):
        # Any non-void value is valid in a boolean context.
        self.check_not_void(left_type, context)
        self.check_not_void(right_type, context)
        # TODO the result type should be the combination of leftType and rightType
        return self.chk.bool_type()
    
    # Check that resultType is compatible with Boolean. It is the return value
    # of the method with the given name (this is used for error message
    # generation).
    void check_boolean_return_value(self, str method, Typ result_type, Context context):
        if not is_subtype(result_type, self.chk.bool_type()):
            self.msg.boolean_return_value_expected(method, context)
    
    # Type check an unary expression ("not", - or ~).
    Typ visit_unary_expr(self, UnaryExpr e):
        operand_type = self.accept(e.expr)
        _x = e.op
        if _x == 'not':
            self.check_not_void(operand_type, e)
            return self.chk.bool_type()
        elif _x == '-':
            method_type = self.analyse_external_member_access('__neg__', operand_type, e)
            return self.check_call(method_type, [], e)
        elif _x == '~':
            method_type = self.analyse_external_member_access('__invert__', operand_type, e)
            return self.check_call(method_type, [], e)
    
    # Type check an index expression (base[index]).
    Typ visit_index_expr(self, IndexExpr e):
        left_type = self.accept(e.base)
        if isinstance(left_type, TupleType):
            # Special case for tuples. They support indexing only by integer
            # literals.
            index = self.unwrap(e.index)
            if isinstance(index, IntExpr):
                n = ((IntExpr)index).value
                tuple_type = (TupleType)left_type
                if n < len(tuple_type.items):
                    return tuple_type.items[n]
                else:
                    self.chk.fail(messages.TUPLE_INDEX_OUT_OF_RANGE, e)
                    return Any()
            else:
                self.chk.fail(messages.TUPLE_INDEX_MUST_BE_AN_INT_LITERAL, e)
                return Any()
        else:
            return self.check_op('__getitem__', left_type, e.index, e)
    
    # Visit a cast expression.
    Typ visit_cast_expr(self, CastExpr expr):
        source_type = self.accept(expr.expr)
        target_type = expr.typ
        if isinstance(target_type, Any):
            return Any()
        else:
            if not self.is_valid_cast(source_type, target_type):
                self.msg.invalid_cast(target_type, source_type, expr)
            return target_type
    
    # Is a cast from sourceType to targetType valid (i.e. can succeed at
    # runtime)?
    bool is_valid_cast(self, Typ source_type, Typ target_type):
        return is_subtype(target_type, source_type) or is_subtype(source_type, target_type) or (isinstance(target_type, Instance) and ((Instance)target_type).typ.is_interface) or (isinstance(source_type, Instance) and ((Instance)source_type).typ.is_interface)
    
    # Type check a type application (expr<...>).
    Typ visit_type_application(self, TypeApplication tapp):
        expr_type = self.accept(tapp.expr)
        if isinstance(expr_type, Callable):
            return self.apply_generic_arguments((Callable)expr_type, tapp.types, [], tapp)
        else:
            self.chk.fail(messages.INVALID_TYPE_APPLICATION_TARGET_TYPE, tapp)
            return Any()
    
    # Type check a list expression [...] or <t> [...].
    Typ visit_list_expr(self, ListExpr e):
        Callable constructor
        if e.typ is not None:
            # A list expression with an explicit item type; translate into type
            # checking a function call.
            constructor = Callable([e.typ],
                                   0,
                                   True,
                                   self.chk.named_generic_type('builtins.list',
                                                               [e.typ]),
                                   False,
                                   '<list>')
        else:
            # A list expression without an explicit type; translate into type
            # checking a generic function call.
            tv = TypeVar('T', -1)
            constructor = Callable([tv],
                                   0,
                                   True,
                                   self.chk.named_generic_type('builtins.list',
                                                               [tv]),
                                   False,
                                   '<list>',
                                   TypeVars([TypeVarDef('T', -1)]))
        return self.check_call(constructor, e.items, e)
    
    # Type check a tuple expression.
    Typ visit_tuple_expr(self, TupleExpr e):    
        if e.types is None:
            TupleType ctx = None
            # Try to determine type context for type inference.
            if isinstance(self.chk.type_context[-1], TupleType):
                t = (TupleType)self.chk.type_context[-1]
                if len(t.items) == len(e.items):
                    ctx = t
            # Infer item types.
            list<Typ> items = []
            for i in range(len(e.items)):
                item = e.items[i]
                Typ tt
                if ctx is None:
                    tt = self.accept(item)
                else:
                    tt = self.accept(item, ctx.items[i])
                self.check_not_void(tt, e)
                items.append(tt)
            return TupleType(items)
        else:
            # Explicit item types, i.e. expression of form <t, ...> (e, ...).
            for j in range(len(e.types)):
                item = e.items[j]
                itemtype = self.accept(item)
                self.chk.check_subtype(itemtype, e.types[j], item,
                                       messages.INCOMPATIBLE_TUPLE_ITEM_TYPE)
            return TupleType(e.types)
    
    Typ visit_dict_expr(self, DictExpr e):
        if e.key_type is None:
            # A dict expression without an explicit type; translate into type
            # checking a generic function call.
            tv1 = TypeVar('KT', -1)
            tv2 = TypeVar('VT', -2)
            Callable constructor
            # The callable type represents a function like this:
            #
            #   dict<kt, vt> make_dict<kt, vt>(tuple<kt, vt> *v): ...
            constructor = Callable([TupleType([tv1, tv2])],
                                   0,
                                   True,
                                   self.chk.named_generic_type('builtins.dict',
                                                               [tv1, tv2]),
                                   False,
                                   '<list>',
                                   TypeVars([TypeVarDef('KT', -1),
                                             TypeVarDef('VT', -2)]))
            # Synthesize function arguments.
            list<Node> args = []
            for key, value in e.items:
                args.append(TupleExpr([key, value]))
            return self.check_call(constructor, args, e)
        else:
            for key_, value_ in e.items:
                kt = self.accept(key_)
                vt = self.accept(value_)
                self.chk.check_subtype(kt, e.key_type, key_, messages.INCOMPATIBLE_KEY_TYPE)
                self.chk.check_subtype(vt, e.value_type, value_, messages.INCOMPATIBLE_VALUE_TYPE)
            return self.chk.named_generic_type('builtins.dict', [e.key_type, e.value_type])
    
    # Type check lambda expression.
    Typ visit_func_expr(self, FuncExpr e):
        # TODO implement properly
        return Any()
    
    # Type check a super expression (non-lvalue).
    Typ visit_super_expr(self, SuperExpr e):
        t = self.analyse_super(e, False)
        return t
    
    # Type check a super expression.
    Typ analyse_super(self, SuperExpr e, bool is_lvalue):
        if e.info is not None and e.info.base is not None:
            return analyse_member_access(e.name, self_type(e.info), e, is_lvalue, True, self.chk.tuple_type(), self.msg, e.info.base)
        else:
            # Invalid super. This has been reported by the semantic analyser.
            return Any()
    
    # Type check a parenthesised expression.
    Typ visit_paren_expr(self, ParenExpr e):
        return self.accept(e.expr, self.chk.type_context[-1])
    
    Typ visit_slice_expr(self, SliceExpr e):
        for index in [e.begin_index, e.end_index, e.stride]:
            if index is not None:
                t = self.accept(index)
                self.chk.check_subtype(t, self.named_type('builtins.int'), index, messages.INVALID_SLICE_INDEX)
        return self.named_type('builtins.slice')
    
    
    # Helpers
    # -------
    
    
    # Type check a node.
    # Alias for TypeChecker accept.
    Typ accept(self, Node node, Typ context=None):
        return self.chk.accept(node, context)
    
    # Generate an error if type is Void.
    void check_not_void(self, Typ typ, Context context):
        self.chk.check_not_void(typ, context)
    
    # Is type compatible with bool?
    bool is_boolean(self, Typ typ):
        return is_subtype(typ, self.chk.bool_type())
    
    # Return an instance type with type given by the name and no type arguments.
    # Alias for TypeChecker namedType.
    Instance named_type(self, str name):
        return self.chk.named_type(name)
    
    # Return the type of a type object.
    #
    # For a generic type G with type variables T and S the type is of form
    #
    #   def <T, S>(...) as G<T, S>,
    #
    # where ... are argument types for the __init__ method.
    Typ type_object_type(self, TypeInfo info):
        if info.is_interface:
            return self.chk.type_type()
        init_method = info.get_method('__init__')
        if init_method is None:
            # Must be an invalid class definition.
            return Any()
        else:
            # Construct callable type based on signature of __init__. Adjust
            # return type and insert type arguments.
            init_type = method_type(init_method)
            if isinstance(init_type, Callable):
                return self.class_callable((Callable)init_type, info)
            else:
                # Overloaded __init__.
                list<Callable> items = []
                for it in ((Overloaded)init_type).items():
                    items.append(self.class_callable(it, info))
                return Overloaded(items)
    
    # Create a callable/overloaded type from the signature of the constructor
    # and the TypeInfo of the class.
    Callable class_callable(self, Callable init_type, TypeInfo info):
        list<TypeVarDef> variables = []
        for i in range(len(info.type_vars)):
            # TODO bounds
            variables.append(TypeVarDef(info.type_vars[i], i + 1, None))
        
        variables.extend(init_type.variables.items)
        
        return Callable(init_type.arg_types,
                        init_type.min_args,
                        init_type.is_var_arg,
                        self_type(info),
                        True,
                        None,
                        TypeVars(variables)).with_name('"{}"'.format(
                                                                 info.name()))
    
    # Is a type valid as a vararg argument?
    bool is_valid_var_arg(self, Typ typ):
        return isinstance(typ, TupleType) or self.is_list_instance(typ) or isinstance(typ, Any)
    
    # Is the argument an instance type list<...>?
    bool is_list_instance(self, Typ t):
        return isinstance(t, Instance) and ((Instance)t).typ.full_name() == 'builtins.list'
    
    # Does a type have a member variable or an accessor with the given name?
    bool has_non_method(self, Typ typ, str member):
        if isinstance(typ, Instance):
            itype = (Instance)typ
            return not itype.typ.has_method(member) and itype.typ.has_readable_member(member)
        else:
            return False
    
    # Unwrap parentheses from an expression node.
    Node unwrap(self, Node e):
        if isinstance(e, ParenExpr):
            return self.unwrap(((ParenExpr)e).expr)
        else:
            return e
    
    # Unwrap parentheses from an expression node.
    list<Node> unwrap_list(self, list<Node> a):
        list<Node> r = []
        for n in a:
            r.append(self.unwrap(n))
        return r


# Return a boolean indicating whether a call expression has a (potentially)
# compatible number of arguments for calling a function. Varargs at caller are
# not checked.
bool is_valid_argc(int nargs, bool is_var_arg, Callable callable):
    if is_var_arg:
        if callable.is_var_arg:
            return True
        else:
            return nargs - 1 <= callable.max_fixed_args()
    elif callable.is_var_arg:
        return nargs >= callable.min_args
    else:
        # Neither has varargs.
        return nargs <= len(callable.arg_types) and nargs >= callable.min_args
