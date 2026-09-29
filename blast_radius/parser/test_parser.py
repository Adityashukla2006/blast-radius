from tree_sitter import Language, Parser
import tree_sitter_python as tspython

# Load Python grammar
PY_LANGUAGE = Language(tspython.language())

# Create parser
parser = Parser(PY_LANGUAGE)

# Python code we want to parse
source = b"""
def add(a, b):
    result = a + b
    return result
"""

# Parse the source code
tree = parser.parse(source)

# Print the tree
print(tree.root_node)