#include "sample.hpp"

int add(int lhs, int rhs) { return lhs + rhs; }

int clamp_to_zero(int value)
{
  if (value < 0) { return 0; }
  return value;
}

bool both(bool lhs, bool rhs) { return lhs && rhs; }

int scale(int value, int factor) { return value * factor; }

int offset(int value) { return value + 1; }// mutate-off

Value *first(Value *values)
{
  return values;
}
