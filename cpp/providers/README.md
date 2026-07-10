# C++ providers

This directory is the native provider bucket for feeds that have a supported
C++ SDK or a measured latency/throughput requirement.

A native provider must normalize into the same bar and trade field contract as
the Python package and use shared conformance fixtures. Providers do not need a
duplicate Python implementation, and provider-specific types must not leak into
`pineforge-engine`.
