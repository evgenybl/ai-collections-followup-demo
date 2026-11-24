"""What every CSV file this project writes does to its cells, so that a spreadsheet opens it as data.

A cell that starts with =, +, -, @, a tab or a carriage return could be run by a spreadsheet as a formula. Such a cell
is written with a leading apostrophe, which a spreadsheet reads as "this is text". The call list, the approved call
list, the generated sheet and the pseudonymised sheet are all written through safe_cells(). The table inside the
request to the model is not: it is text for the model, and no spreadsheet opens it.

Standard library only.
"""

FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def safe_cells(cells):
    """A row's cells as they are written to a CSV file, and how many of them were given a leading apostrophe."""
    cells = [str(cell) for cell in cells]
    safe = ["'" + cell if cell.startswith(FORMULA_START) else cell for cell in cells]
    return safe, sum(1 for before, after in zip(cells, safe) if before != after)
