# Issues List

This file lists the problems in blast-radius and how we fix them.

**What does blast-radius do?**
You change some code. The tool tells you which other code might break because of your change.
It does this by making a map of "who calls who". We call this map the **graph**.
Each function is a dot. Each arrow means "this function calls that function".

**Status words:**
- ✅ **Fixed** – done and tested.
- 🔧 **Working on it** – we are fixing it right now.
- ⏳ **To do** – not started yet.
- 🤷 **We accept it** – it is a small problem, and fixing it costs too much.

---

## 🔧 Working on it

Nothing right now.

---

## ✅ Fixed

### 1. The tool used the wrong names for some files
**What was wrong:**
Every file gets a name. The tool makes the name from the folder path.
The file `backend/app/db.py` gets the name `backend.app.db`.
But the code itself says `from app.db import query`. It calls the file `app.db`.
The two names do not match, so the tool can't find the file. It thinks `app` is some outside library.

**Why it happens:**
The project is started from inside the `backend` folder. So, for Python, `backend` is not part of the name.
The tool did not know this. It always started the name at the top folder of the repo.

**Why it is bad:**
In the cat-hackathon repo, almost every import is like this. So the tool misses almost every arrow between files.

**How we fixed it:**
A folder with an `__init__.py` file inside is a Python "package". The name starts at the highest package folder.
`backend/app/` has `__init__.py`. `backend/` does not. So the name starts at `app`.
We keep a small "nickname list": `app.db` → `backend.app.db`.
When the code says `app.db`, the tool looks in the nickname list and finds the right file.
If two files want the same nickname, we don't use that nickname at all. A wrong arrow is worse than no arrow.

**Did it work?** Yes. In cat-hackathon, the arrows between files went from 0 to 362. All the arrows together went from 199 to 519.

**Small limit:** Some packages have no `__init__.py` file. The fix can't see those yet.

### 2. The tool read every file, every time
**What was wrong:** Each run read and understood (parsed) every file again. On a big repo this is very slow.

**How we fixed it:** We keep a "memory box" (a cache) inside the `.git` folder. It remembers what each file looked like.
For each file, the tool checks in this order, cheapest first:
1. Is the file's time and size the same as last time? → Use the memory. Don't even open the file.
2. If not, is the content exactly the same? (We check with a hash, which is like a fingerprint. Making a fingerprint is about 30 times faster than parsing.) → Use the memory.
3. If not → parse the file again.

If we change the parser code, the memory box empties itself, so it never gives old answers.

### 3. The tool read files it should skip
**What was wrong:** The tool read every `.py` file in the folder, even ones git ignores. Our test repo clone in `bench/targets/` got mixed into the map.

**How we fixed it:** We ask git for the list of files (`git ls-files`). Git already knows which files to ignore.

### 4. The search stopped at a fixed number of steps
**What was wrong:** The search for affected code stopped after 6 arrows. That made the code complex. It also kept results that were very unlikely, and the "depth" it showed was confusing.

**How we fixed it:** Now the search stops when the chance gets too small (below 0.05), not after a number of steps.
Each arrow makes the chance smaller. So the search still stops by itself, after about 9 strong arrows.

### 5. Small code mistakes
- `resolver.py` used `Expression` but never imported it.
- `index()` filled two tables two times.
- `main()` had a path that only worked on one computer.

All fixed.

### 6. There were no tests
**How we fixed it:** We added a `tests/` folder with tests for the search and for the memory box.

### 7. `self.method()` calls got no arrow
**What was wrong:** Inside a class, `self.save()` calls another method of the same class. The tool skipped these calls.
So if you changed a method, the tool could say nothing breaks.

**How we fixed it:**
- `self.save()` and `cls.make()`: the tool finds the class the caller lives in, and looks for the method there (label `self`).
  If the class doesn't have it, it looks in the parent classes (label `inherited`).
- `super().save()`: the tool skips the caller's own class and looks only in the parents (label `inherited`).
- Child classes can replace a method. Then `self.validate()` can run the child's version.
  So the tool also draws a weaker arrow to every child's version (label `override`).
  This also works when only the children have the method, and for grandchildren.

**Did it work?** Yes. In cat-hackathon, 27 of the 28 `self` calls now get an arrow.
The last one, `self._sleep()`, is not a method. It is a function stored in a variable, so the tool is right to skip it.

### 8. `module.func()` calls got no arrow
**What was wrong:** `import db` then `db.query()` – the tool skipped every call with a dot in it.

**How we fixed it:** The tool now reads the name before the dot one piece at a time.
`app.db.query` → first find `app`, then `db` inside it, then `query` inside that.
Each piece can be a file, a class, or a function. If a piece is a local variable or an outside library, the tool stops and doesn't guess.

### 9. `Foo()` did not connect to anything
**What was wrong:** Making a new object runs its `__init__` method, but the tool didn't know that.
Also, classes were not dots on the map. So changing a line inside a class (not inside a method) looked like no change at all.

**How we fixed it:**
- Every class is now a dot on the map.
- `Foo()` draws an arrow to the class `Foo`, and also to its `__init__` (or to the parent class's `__init__`).
- A class like `class Student(User)` draws an arrow `Student` → `User`, labelled `inherits`.
  So if you change `User`, the tool also finds the code that uses `Student`.
- `User.create()` draws an arrow to the method `User.create`, also if a parent class has it.

### 10. Imports that passed through `__init__.py` got lost
**What was wrong:** `pkg/__init__.py` says `from .core import run`. Another file says `from pkg import run`.
The tool looked for `run` in `pkg` only, and didn't follow it to `pkg.core`.

**How we fixed it:** A new lookup follows each import step by step until it finds where the name is really written.
It also follows `import *`. It remembers every place it has been, so it can't go round in circles.

**Did it work? (issues 8, 9 and 10 together)** Yes. In cat-hackathon:
- The arrows went from 518 to 860.
- Calls the tool can't place went from 78% to 66%.
- New arrows: 293 `constructor`, 24 `module`, 9 `class`, 16 `inherits`.

### 11. Calls on normal variables got no arrow
**What was wrong:** `user = User()` then `user.save()`. The tool didn't know what kind of thing `user` is, so it drew no arrow.

**How we fixed it – part 1, look for clues:**
The tool looks in the code for a clue that tells it what the variable is:
- `user = User()` – it is made from a class
- `def f(user: User)` or `user: User = ...` – a type hint (also `User | None` and `Optional[User]`)
- `user = load()`, where `def load() -> User` – the function says what it gives back
- `self.db = Database()`, or `db: Database` in the class body – for `self.db.query()`
- `tracker = Tracker()` at the top of the file
- `b = a` – b is the same kind of thing as a
If the clue points to something from outside the repo (like `db: Session` from SQLAlchemy), the tool draws no arrow. That is correct.

**How we fixed it – part 2, guess (can be switched off):**
If there is no clue at all, and exactly one class in the repo has a method with that name, the tool guesses that one.
It never guesses names like `get` or `append`, because normal lists and dicts have those too.
Guessed arrows get a low score (0.3). Switch it off with `Resolver(repo, guess=False)` or `measure.py --no-guess`.

**Did it work?** Yes. In cat-hackathon:
- Part 1 (clues): 87 more calls got an arrow.
- Part 2 (guess): 46 more calls got an arrow. We checked them: they were right.
  Most were test fixtures like `predictor` and `fake_llm`, which have no type hints.

### 26. `get_client().send()` looked like a plain `send()`
**What was wrong:**
When the part before the dot was not a simple name, the tool threw it away.
So `get_client().send()` looked just like `send()`. If the file had its own function called `send`, the tool drew an arrow to it. That arrow was wrong.

**How we fixed it:** Now the tool writes down `<expr>` for that part. It knows it is not a plain call, so it does not guess.

**Did it work?** Yes. In cat-hackathon, 341 calls were wrongly counted as plain calls. Now they are not. One wrong arrow went away.

### 24. A secret was inside `blast_radius/resolver/ok.py`
This file had nothing to do with the project, and it had a secret token in it.
It is deleted now, and it was never saved into git.

---

## ⏳ To do

### 12. Two functions with the same name squash each other
A `@property` getter and its setter have the same name. The second one overwrites the first one on the map.
**Plan:** Give the second one a different name, like adding its line number.

### 13. A function, a class and a variable with the same name become one dot
`handler = wrap(handler)` – the function `handler` and the variable `handler` get the same name, so they merge.
**Plan:** Give each kind its own name.

### 14. Variables have no arrows
Changing `TIMEOUT = 5` should affect every function that uses `TIMEOUT`. Right now the tool says nothing breaks.
**Plan:** Record which functions read which variables.

### 15. Functions passed around are not seen
`Thread(target=work)` or `map(fn, items)` – the function is handed over, not called directly. No arrow.

### 16. Calls outside of functions are ignored
`app = create_app()` at the top of a file gets no arrow.

### 17. Deleted functions are not reported
The tool only looks at the new version of a file. A deleted function is not on the new map, so it can't be found.
Also, when lines are deleted, the tool marks the line just before. That can be a different function.
**Plan:** Also read the old version of the file from git.

### 18. Brand-new files are missed in the change list
`git diff` doesn't show files that git isn't tracking yet. New files you haven't `git add`-ed are missed.

### 19. There is no command to run the tool
`pyproject.toml` says there is a `blast-radius` command, but the file for it (`cli.py`) doesn't exist.
**Plan:** Make `cli.py`. Add options like `--base main` and an option to run only the affected tests.

### 20. We don't know how accurate the tool is
**Plan:** Break one function on purpose in a test repo. Run all the tests. Check: did the tool pick the tests that failed?
This gives us a score. Then we can see if each fix really helps.

### 21. Very big repos will still be slow in some places
- The memory box is saved as one big file. Changing one file rewrites the whole thing.
- Making the arrows (`link()`) still goes through every file each time.
**Plan:** Use a small database (SQLite), one row per file. Also remember the arrows, and only redo the changed files.

### 22. Only one arrow is kept between two functions
If `a` calls `b` on three lines, the map keeps just the last line. This is small. It only matters for showing line numbers.

### 27. Test fixtures could tell us the exact type
In pytest, `def test_x(predictor):` gets `predictor` from a fixture function in `conftest.py`.
That function often says what it gives back (`-> FakeLLM`), or it makes the object (`return ShadowPredictor.load()`).
Right now the tool only guesses these by method name.
**Plan:** Match the test's parameter name to the fixture function, and use what it gives back as the type.

### 28. Some variables are still unknown
These are not covered yet:
- `for user in users:` and `with open(...) as f:`
- two steps on a variable, like `user.profile.save()`
- `get_client().send()` – the function's return type could tell us what `get_client()` gives back

### 23. A local function always wins over an imported one with the same name
In Python, whichever comes later in the file wins. The tool always picks the local one. This is rare.

---

## 🤷 We accept it

### 25. The memory box can miss a very fast change
If a file changes but keeps the exact same time and size, the memory box uses the old answer.
This almost never happens. Git itself has the same small problem and accepts it too.
