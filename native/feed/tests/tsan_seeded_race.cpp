// SPDX-License-Identifier: Apache-2.0
#include <cstdio>
#include <thread>

namespace {
void seeded_race(int& value) {
    for (int iteration = 0; iteration < 10000; ++iteration) {
        ++value;  // Deliberate unsynchronized read and write; TSan must report this.
        std::this_thread::yield();
    }
}
}

int main() {
    int value = 0;
    std::thread first([&] { seeded_race(value); });
    std::thread second([&] { seeded_race(value); });
    first.join();
    second.join();
    std::printf("Seeded race finished with value %d\n", value);
    return 0;
}
