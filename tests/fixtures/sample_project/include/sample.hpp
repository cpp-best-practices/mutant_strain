#ifndef SAMPLE_HPP
#define SAMPLE_HPP

using Value = int;

[[nodiscard]] int add(int lhs, int rhs);
[[nodiscard]] int clamp_to_zero(int value);
[[nodiscard]] bool both(bool lhs, bool rhs);
[[nodiscard]] int scale(int value, int factor);
[[nodiscard]] int offset(int value);
[[nodiscard]] Value *first(Value *values);

#endif
