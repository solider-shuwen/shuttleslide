"""
DrawingML Formula Engine

Calculates formula values according to ECMA-376 Part 1, 20.1.10.55.
Supports all DrawingML formula operators.
"""

import math
import re
from typing import Dict, List, Optional, Set, Tuple


class FormulaEngine:
    """
    DrawingML formula calculation engine.

    Evaluates formulas defined in gdLst (guide list) elements.
    Supports dependency resolution and topological sorting.
    """

    # DrawingML angle constants in ST_Angle units (1/60000 degree per ECMA-376)
    ANGLE_CONSTANTS = {
        'cd2': 10800000,     # 180 degrees  (180 * 60000)
        'cd4': 5400000,      #  90 degrees  (90 * 60000)
        'cd8': 2700000,      #  45 degrees  (45 * 60000)
        '3cd4': 16200000,    # 270 degrees  (270 * 60000)
        '3cd8': 8100000,     # 135 degrees  (135 * 60000)
        '5cd8': 13500000,    # 225 degrees  (225 * 60000)
        '7cd8': 18900000,    # 315 degrees  (315 * 60000)
    }

    def __init__(self):
        """Initialize the formula engine."""
        self.context: Dict[str, float] = {}
        self.evaluated: Dict[str, float] = {}

    def evaluate_formula(self, formula: str, context: Dict[str, float]) -> float:
        """
        Evaluate a single formula.

        Args:
            formula: Formula string (e.g., "val 12345", "*/ h adj1 100000")
            context: Variable context dictionary

        Returns:
            Calculated value
        """
        if not formula:
            return 0.0

        # Split formula into parts
        parts = formula.strip().split()
        if not parts:
            return 0.0

        operator = parts[0]
        args = parts[1:]

        return self._evaluate_operator(operator, args, context)

    def evaluate_formulas(
        self,
        gd_lst: List[Dict],
        av_lst: Dict[str, int],
        shape_context: Dict[str, float]
    ) -> Dict[str, float]:
        """
        Evaluate all formulas in dependency order.

        Args:
            gd_lst: List of formula definitions from gdLst
            av_lst: Adjustment values from avLst
            shape_context: Shape context (l, t, r, b, w, h, etc.)

        Returns:
            Dictionary of evaluated formula values
        """
        self.evaluated = {}
        # shape_context contains merged adjustments (user overrides defaults),
        # so it must take priority over av_lst (defaults only)
        self.context = {**av_lst, **shape_context}

        # Add angle constants to context
        self.context.update(self.ANGLE_CONSTANTS)

        # Build dependency graph
        dependencies = self._build_dependency_graph(gd_lst)

        # Topological sort and evaluate
        remaining = gd_lst.copy()
        iterations = 0
        max_iterations = len(gd_lst) * 2  # Prevent infinite loops

        while remaining and iterations < max_iterations:
            iterations += 1

            for i, gd in enumerate(remaining):
                name = gd['name']
                formula = gd['formula']

                # Check if all dependencies are satisfied
                deps = dependencies.get(name, set())
                available_vars = set(self.context.keys()) | set(self.evaluated.keys())

                if deps.issubset(available_vars):
                    # Merge context for evaluation
                    eval_context = {**self.context, **self.evaluated}
                    value = self.evaluate_formula(formula, eval_context)

                    # Store result
                    self.evaluated[name] = value
                    self.context[name] = value  # Make available to subsequent formulas

                    # Remove from remaining
                    remaining.pop(i)
                    break

        return self.evaluated

    def _build_dependency_graph(self, gd_lst: List[Dict]) -> Dict[str, Set[str]]:
        """
        Build dependency graph for formulas.

        Args:
            gd_lst: List of formula definitions

        Returns:
            Dictionary mapping formula name to set of dependencies
        """
        dependencies = {}

        for gd in gd_lst:
            name = gd['name']
            formula = gd['formula']
            deps = self._extract_dependencies(formula)
            dependencies[name] = deps

        return dependencies

    def _extract_dependencies(self, formula: str) -> Set[str]:
        """
        Extract variable names from a formula.

        Args:
            formula: Formula string

        Returns:
            Set of variable names used in the formula
        """
        parts = formula.split()[1:]  # Skip operator
        deps = set()

        for part in parts:
            # Check if it's a variable name (not a number)
            if part and not re.match(r'^-?\d+\.?\d*$', part):
                deps.add(part)

        return deps

    def _evaluate_operator(
        self,
        operator: str,
        args: List[str],
        context: Dict[str, float]
    ) -> float:
        """
        Evaluate a formula operator.

        Args:
            operator: Operator name
            args: Operator arguments
            context: Variable context

        Returns:
            Calculated value
        """
        handlers = {
            'val': self._op_val,
            '*/': self._op_mul_div,
            '+-': self._op_add_sub,
            '+/': self._op_add_div,
            '*/+': self._op_mul_div_add,
            '+': self._op_add,
            '-': self._op_sub,
            '*': self._op_mul,
            '/': self._op_div,
            '?': self._op_if,
            'abs': self._op_abs,
            'at2': self._op_atan2,
            'cos': self._op_cos,
            'sin': self._op_sin,
            'tan': self._op_tan,
            'cat2': self._op_cat2,
            'sat2': self._op_sat2,
            'sat': self._op_sat,
            'min': self._op_min,
            'max': self._op_max,
            'sqrt': self._op_sqrt,
            'mod': self._op_mod,
            'pin': self._op_pin,
        }

        if operator not in handlers:
            return 0.0

        return handlers[operator](args, context)

    def _resolve_value(self, value: str, context: Dict[str, float]) -> float:
        """
        Resolve a value from context or parse as number.

        Args:
            value: Variable name or numeric string
            context: Variable context

        Returns:
            Resolved float value
        """
        # Check if it's in context
        if value in context:
            return float(context[value])

        # Try to parse as number
        try:
            return float(value)
        except ValueError:
            return 0.0

    # ── Operator implementations ─────────────────────────────────────

    def _op_val(self, args: List[str], context: Dict[str, float]) -> float:
        """Constant value: val 12345 -> 12345"""
        if not args:
            return 0.0
        return self._resolve_value(args[0], context)

    def _op_mul_div(self, args: List[str], context: Dict[str, float]) -> float:
        """Multiply then divide: */ a b c -> (a * b) / c"""
        if len(args) < 3:
            return 0.0
        a = self._resolve_value(args[0], context)
        b = self._resolve_value(args[1], context)
        c = self._resolve_value(args[2], context)
        if c == 0:
            return 0.0
        return (a * b) / c

    def _op_add_sub(self, args: List[str], context: Dict[str, float]) -> float:
        """Add then subtract: +- a b c -> a + b - c"""
        if len(args) < 1:
            return 0.0
        result = self._resolve_value(args[0], context)

        # Process pairs: (b, c), (d, e), etc.
        i = 1
        while i + 1 < len(args):
            b = self._resolve_value(args[i], context)
            c = self._resolve_value(args[i + 1], context)
            result = result + b - c
            i += 2

        return result

    def _op_add_div(self, args: List[str], context: Dict[str, float]) -> float:
        """Add then divide: +/ a b c -> (a + b) / c"""
        if len(args) < 3:
            return 0.0
        a = self._resolve_value(args[0], context)
        b = self._resolve_value(args[1], context)
        c = self._resolve_value(args[2], context)
        if c == 0:
            return 0.0
        return (a + b) / c

    def _op_mul_div_add(self, args: List[str], context: Dict[str, float]) -> float:
        """Multiply, divide, then add: */+ a b c d -> (a * b) / c + d"""
        if len(args) < 4:
            return 0.0
        a = self._resolve_value(args[0], context)
        b = self._resolve_value(args[1], context)
        c = self._resolve_value(args[2], context)
        d = self._resolve_value(args[3], context)
        if c == 0:
            return 0.0
        return (a * b) / c + d

    def _op_add(self, args: List[str], context: Dict[str, float]) -> float:
        """Addition: + a b -> a + b"""
        if len(args) < 2:
            return 0.0
        return self._resolve_value(args[0], context) + self._resolve_value(args[1], context)

    def _op_sub(self, args: List[str], context: Dict[str, float]) -> float:
        """Subtraction: - a b -> a - b"""
        if len(args) < 2:
            return 0.0
        return self._resolve_value(args[0], context) - self._resolve_value(args[1], context)

    def _op_mul(self, args: List[str], context: Dict[str, float]) -> float:
        """Multiplication: * a b -> a * b"""
        if len(args) < 2:
            return 0.0
        return self._resolve_value(args[0], context) * self._resolve_value(args[1], context)

    def _op_div(self, args: List[str], context: Dict[str, float]) -> float:
        """Division: / a b -> a / b"""
        if len(args) < 2:
            return 0.0
        b = self._resolve_value(args[1], context)
        if b == 0:
            return 0.0
        return self._resolve_value(args[0], context) / b

    def _op_if(self, args: List[str], context: Dict[str, float]) -> float:
        """Conditional: ?: cond true_val false_val"""
        if len(args) < 3:
            return 0.0
        cond = self._resolve_value(args[0], context)
        true_val = self._resolve_value(args[1], context)
        false_val = self._resolve_value(args[2], context)
        return true_val if cond else false_val

    def _op_abs(self, args: List[str], context: Dict[str, float]) -> float:
        """Absolute value: abs a -> |a|"""
        if not args:
            return 0.0
        return abs(self._resolve_value(args[0], context))

    def _op_atan2(self, args: List[str], context: Dict[str, float]) -> float:
        """Arc tangent: at2 x y -> atan2(y, x) in ST_Angle (1/60000 degree)"""
        if len(args) < 2:
            return 0.0
        x = self._resolve_value(args[0], context)
        y = self._resolve_value(args[1], context)
        return math.atan2(y, x) * 10800000 / math.pi

    def _op_cos(self, args: List[str], context: Dict[str, float]) -> float:
        """Cosine: cos x y -> x * cos(y) where y is in ST_Angle (1/60000 degree)"""
        if not args:
            return 0.0
        a = self._resolve_value(args[0], context)
        # Convert from ST_Angle (1/60000 degree) to radians
        radians = a * math.pi / 10800000
        return math.cos(radians)

    def _op_sin(self, args: List[str], context: Dict[str, float]) -> float:
        """Sine: sin x y -> x * sin(y) where y is in ST_Angle (1/60000 degree)"""
        if not args:
            return 0.0
        a = self._resolve_value(args[0], context)
        # Convert from ST_Angle (1/60000 degree) to radians
        radians = a * math.pi / 10800000
        return math.sin(radians)

    def _op_tan(self, args: List[str], context: Dict[str, float]) -> float:
        """Tangent: tan a -> tan(a) where a is in ST_Angle (1/60000 degree)"""
        if not args:
            return 0.0
        a = self._resolve_value(args[0], context)
        # Convert from ST_Angle (1/60000 degree) to radians
        radians = a * math.pi / 10800000
        return math.tan(radians)

    def _op_max(self, args: List[str], context: Dict[str, float]) -> float:
        """Maximum: max a b -> max(a, b)"""
        if len(args) < 2:
            return 0.0
        return max(
            self._resolve_value(args[0], context),
            self._resolve_value(args[1], context)
        )

    def _op_min(self, args: List[str], context: Dict[str, float]) -> float:
        """Minimum: min a b -> min(a, b)"""
        if len(args) < 2:
            return 0.0
        return min(
            self._resolve_value(args[0], context),
            self._resolve_value(args[1], context)
        )

    def _op_sqrt(self, args: List[str], context: Dict[str, float]) -> float:
        """Square root: sqrt a -> sqrt(a)"""
        if not args:
            return 0.0
        val = self._resolve_value(args[0], context)
        if val < 0:
            return 0.0
        return math.sqrt(val)

    def _op_mod(self, args: List[str], context: Dict[str, float]) -> float:
        """Vector modulus: mod a b [c] -> sqrt(a² + b² [+ c²])"""
        if len(args) < 2:
            return 0.0
        a = self._resolve_value(args[0], context)
        b = self._resolve_value(args[1], context)
        if len(args) >= 3:
            c = self._resolve_value(args[2], context)
            return math.sqrt(a * a + b * b + c * c)
        return math.sqrt(a * a + b * b)

    def _op_pin(self, args: List[str], context: Dict[str, float]) -> float:
        """Pin to range: pin val min max -> clamp(val, min, max)"""
        if len(args) < 3:
            return 0.0
        val = self._resolve_value(args[0], context)
        min_val = self._resolve_value(args[1], context)
        max_val = self._resolve_value(args[2], context)
        return max(min_val, min(max_val, val))

    def _op_sat(self, args: List[str], context: Dict[str, float]) -> float:
        """Saturate: sat val min max -> clamp(val, min, max)"""
        # Same as pin
        return self._op_pin(args, context)

    def _op_cat2(self, args: List[str], context: Dict[str, float]) -> float:
        """Cosine-ArcTan2: cat2 a b c -> a * cos(atan2(c, b))"""
        if len(args) < 3:
            return 0.0
        a = self._resolve_value(args[0], context)
        b = self._resolve_value(args[1], context)
        c = self._resolve_value(args[2], context)
        return a * math.cos(math.atan2(c, b))

    def _op_sat2(self, args: List[str], context: Dict[str, float]) -> float:
        """Sine-ArcTan2: sat2 a b c -> a * sin(atan2(c, b))"""
        if len(args) < 3:
            return 0.0
        a = self._resolve_value(args[0], context)
        b = self._resolve_value(args[1], context)
        c = self._resolve_value(args[2], context)
        return a * math.sin(math.atan2(c, b))
