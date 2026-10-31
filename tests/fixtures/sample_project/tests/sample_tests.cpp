#include "sample.hpp"

int main()
{
  int values[] = { 1, 2 };
  if (add(2, 3) != 5) { return 1; }
  if (scale(5, 0) != 0) { return 1; }
  if (offset(1) != 2) { return 1; }
  if (first(values) != values) { return 1; }
  static_cast<void>(clamp_to_zero(5));
  static_cast<void>(both(true, true));
  return 0;
}
