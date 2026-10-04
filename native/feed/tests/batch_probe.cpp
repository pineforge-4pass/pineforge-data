// SPDX-License-Identifier: Apache-2.0
#include <pineforge/pineforge.h>
#include <dlfcn.h>
#include <fstream>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

template<class Function> Function load(void* library, const char* name) {
    auto function = reinterpret_cast<Function>(dlsym(library, name));
    if (!function) throw std::runtime_error(std::string("missing qualification export: ") + name);
    return function;
}

int main(int argc, char** argv) {
    try {
        if (argc != 4 && argc != 5)
            throw std::runtime_error("usage: batch-probe observed-strategy.so bars.csv actions.jsonl [qty_step]");
        std::ifstream input(argv[2]);
        if (!input) throw std::runtime_error("cannot open batch input");
        std::string line;
        std::getline(input, line);
        if (line != "timestamp,open,high,low,close,volume") throw std::runtime_error("unexpected CSV schema");
        std::vector<pf_bar_t> bars;
        while (std::getline(input, line)) {
            std::istringstream fields(line);
            std::vector<std::string> values;
            std::string value;
            while (std::getline(fields, value, ',')) values.push_back(value);
            if (values.size() != 6) throw std::runtime_error("invalid CSV row");
            bars.push_back({std::stod(values[1]), std::stod(values[2]), std::stod(values[3]),
                std::stod(values[4]), std::stod(values[5]), std::stoll(values[0])});
        }
        void* library = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
        if (!library) throw std::runtime_error(dlerror());
        if (load<decltype(&pf_abi_version)>(library, "pf_abi_version")() != PF_ABI_VERSION)
            throw std::runtime_error("qualification strategy ABI mismatch");
        auto create = load<decltype(&strategy_create)>(library, "strategy_create");
        auto release = load<decltype(&strategy_free)>(library, "strategy_free");
        auto report_release = load<decltype(&report_free)>(library, "report_free");
        auto run = load<decltype(&run_backtest_full)>(library, "run_backtest_full");
        auto retain = load<int(*)(void*)>(library, "equivalence_retain_events");
        auto actions = load<int(*)(void*, const char*)>(library, "equivalence_export_actions");
        auto error = load<decltype(&strategy_get_last_error)>(library, "strategy_get_last_error");
        auto strategy = create(nullptr);
        if (!strategy || retain(strategy)) throw std::runtime_error("cannot retain batch execution receipts");
        // The runner's --syminfo qty_step=V, read the same way (std::stod).
        if (argc == 5) load<decltype(&strategy_set_syminfo_metadata)>(library, "strategy_set_syminfo_metadata")(
            strategy, "qty_step", std::stod(argv[4]));
        pf_report_t report{};
        run(strategy, bars.data(), static_cast<int>(bars.size()), "1", "1", 0, 4, PF_MAGNIFIER_ENDPOINTS, &report);
        const auto* failure = error(strategy);
        if (failure && *failure) throw std::runtime_error(failure);
        if (actions(strategy, argv[3])) throw std::runtime_error("cannot export batch execution actions");
        std::cout << "batch_abi=run_backtest_full input_bars=" << bars.size()
                  << " script_bars=" << report.script_bars_processed << " trades=" << report.trades_len << '\n';
        report_release(&report);
        release(strategy);
        dlclose(library);
        return 0;
    } catch (const std::exception& failure) {
        std::cerr << failure.what() << '\n';
        return 1;
    }
}
