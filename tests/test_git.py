import unittest

from strain.git import parse_changed_lines

DIFF = """\
diff --git a/src/math.cpp b/src/math.cpp
index 1111111..2222222 100644
--- a/src/math.cpp
+++ b/src/math.cpp
@@ -3,0 +4,2 @@ int f()
+  a
+  b
@@ -10 +12 @@ int g()
-old
+new
@@ -20,3 +21,0 @@ int h()
-gone
diff --git a/src/new.cpp b/src/new.cpp
new file mode 100644
--- /dev/null
+++ b/src/new.cpp
@@ -0,0 +1,3 @@
+x
+y
+z
diff --git a/src/removed.cpp b/src/removed.cpp
deleted file mode 100644
--- a/src/removed.cpp
+++ /dev/null
@@ -1 +0,0 @@
-x
"""


class ParseChangedLinesTest(unittest.TestCase):
    def test_added_and_modified_lines_per_file(self) -> None:
        self.assertEqual(
            parse_changed_lines(DIFF),
            {"src/math.cpp": {4, 5, 12}, "src/new.cpp": {1, 2, 3}},
        )


if __name__ == "__main__":
    unittest.main()
