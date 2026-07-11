// Crash handler for xrpld / rippled-fuzzer, installed process-wide via
// /etc/ld.so.preload (see Dockerfile.xrpld) so it loads into every node built on
// the image — validators and the tracking-node peer launch xrpld directly with no
// entrypoint script, so an LD_PRELOAD env set only in fuzzer-entrypoint.sh would
// miss them.
//
// On a fatal signal (SIGSEGV/SIGABRT/SIGBUS/SIGFPE/SIGILL) it writes an
// async-signal-safe raw stack dump, then restores the default disposition and
// re-raises — so, with core dumps enabled (compose `ulimits: core`), the crash
// ALSO leaves a core. On the next start it symbolizes any leftover dump into
// stderr (which the container captures) as file:line frames.
//
// Why two phases: symbolizing a stack to text allocates and shells out to
// addr2line, neither of which is async-signal-safe. boost::stacktrace's
// safe_dump_to() only records raw return addresses (signal-safe); from_dump()
// does the unsafe symbolization later, in a healthy process.
//
// Symbolization on restart assumes stable module load addresses between the
// crashing and recovering runs. Antithesis runs deterministically with ASLR
// disabled, so the absolute addresses in the dump resolve correctly. The
// re-raised core dump is the ASLR-independent fallback: open it in gdb against
// /symbols/xrpld (the binary ships full DWARF).
//
// Built header-only with the addr2line backend (-DBOOST_STACKTRACE_USE_ADDR2LINE
// + -ldl); see Dockerfile.xrpld. Linked -static-libstdc++/-static-libgcc to
// match rippled and avoid interposing a second dynamic C++ runtime.

#define _GNU_SOURCE 1

#include <boost/stacktrace.hpp>

#include <cerrno>  // program_invocation_short_name
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>

#include <unistd.h>

namespace {

char g_dump_path[4096] = {0};

// async-signal-safe write of a NUL-terminated string
void safe_write(int fd, char const* s)
{
    ssize_t n = ::write(fd, s, std::strlen(s));
    (void)n;
}

extern "C" void crash_handler(int signum)
{
    // Restore the default so the re-raise performs the normal action
    // (terminate + core dump) instead of re-entering this handler.
    std::signal(signum, SIG_DFL);

    safe_write(STDERR_FILENO, "\n*** fatal signal caught \xe2\x80\x94 dumping stack to ");
    safe_write(STDERR_FILENO, g_dump_path);
    safe_write(STDERR_FILENO, " ***\n");

    // Signal-safe: records raw return addresses only, no symbolization.
    boost::stacktrace::safe_dump_to(g_dump_path);

    std::raise(signum);
}

void symbolize_previous_dump()
{
    std::ifstream in(g_dump_path, std::ios::binary);
    if (!in.good())
        return;

    // No const char* overload in boost::stacktrace::from_dump — it takes an
    // istream (reads the raw frame pointers written by safe_dump_to).
    boost::stacktrace::stacktrace const st =
        boost::stacktrace::stacktrace::from_dump(in);
    in.close();

    // Write via ::write rather than std::cerr: this runs from the preload's
    // constructor, before main(), where the std::cerr global's init ordering
    // relative to ours isn't guaranteed.
    std::ostringstream oss;
    oss << "=== recovered crash backtrace (from " << g_dump_path << ") ===\n"
        << st << "=== end crash backtrace ===\n";
    std::string const out = oss.str();
    ssize_t const n = ::write(STDERR_FILENO, out.data(), out.size());
    (void)n;

    // Remove so the same crash isn't re-reported on every later start.
    std::remove(g_dump_path);
}

__attribute__((constructor)) void install()
{
    // Per-process dump path so xrpld and rippled-fuzzer don't clobber each
    // other. Overridable with XRPLD_STACKDUMP (a full path).
    char const* env = std::getenv("XRPLD_STACKDUMP");
    if (env && *env)
    {
        std::snprintf(g_dump_path, sizeof(g_dump_path), "%s", env);
    }
    else
    {
        std::snprintf(
            g_dump_path,
            sizeof(g_dump_path),
            "/var/log/xrpld/cores/%s.stackdump",
            program_invocation_short_name);
    }

    // Report (and clear) a dump left by a previous crashed run, if any.
    symbolize_previous_dump();

    for (int const s : {SIGSEGV, SIGABRT, SIGBUS, SIGFPE, SIGILL})
        std::signal(s, &crash_handler);
}

}  // namespace
