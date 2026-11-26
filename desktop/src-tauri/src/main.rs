// No console window on Windows release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    // Installer smoke test (#58): start the bundled engine on a throwaway config, check it,
    // stop it, exit 0 or 1. No window. The report also goes to the file named after the flag,
    // since a Windows GUI build has no console to print to.
    let args: Vec<String> = std::env::args().collect();
    if let Some(i) = args.iter().position(|a| a == "--check-bundled-engine") {
        std::process::exit(opencap_desktop::check_bundled_engine(
            args.get(i + 1).map(String::as_str),
        ));
    }
    opencap_desktop::run()
}
