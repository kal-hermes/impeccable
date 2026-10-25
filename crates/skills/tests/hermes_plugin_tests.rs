//! Hermes design-hook plugin install: the `.hermes` provider has no hook
//! manifest — `copy_provider_hooks` copies the bundled `hermes-plugin/`
//! payload into the Hermes plugin home instead, and `install`/`check`/`update`
//! treat "both plugin files exist" as the installed marker.

use impeccable_skills::hook_manifest::*;
use impeccable_skills::providers::Sys;
use std::collections::HashMap;

fn tmp_dir(name: &str) -> std::path::PathBuf {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let dir = std::env::temp_dir().join(format!("imp-hermes-{name}-{}-{nanos}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn sys_with_home(home: &std::path::Path) -> Sys {
    let mut env = HashMap::new();
    env.insert("HOME".to_string(), home.to_string_lossy().into_owned());
    Sys::new(env, home.to_string_lossy().into_owned())
}

/// A minimal bundle: the hermes skill variant carrying the plugin payload.
fn seed_bundle(root: &std::path::Path) -> String {
    let payload = root.join("bundle/.hermes/skills/impeccable/hermes-plugin");
    std::fs::create_dir_all(&payload).unwrap();
    std::fs::write(payload.join("plugin.yaml"), "name: impeccable-design-hook\n").unwrap();
    std::fs::write(payload.join("__init__.py"), "def register(ctx):\n    pass\n").unwrap();
    root.join("bundle").to_string_lossy().into_owned()
}

#[test]
fn hermes_plugin_installs_into_home_plugins_dir() {
    let root = tmp_dir("install");
    let home = root.join("home");
    let bundle = seed_bundle(&root);
    let sys = sys_with_home(&home);

    assert!(!hermes_plugin_installed(&sys));

    let dest = hermes_plugin_dest(&sys);
    assert!(dest.contains("plugins/impeccable-design-hook"), "{dest}");
    assert!(dest.starts_with(home.to_string_lossy().as_ref()), "{dest}");

    let written = install_hermes_plugin(&sys, &bundle, false).unwrap();
    assert!(written);
    for file in HERMES_PLUGIN_FILES {
        assert!(
            std::path::Path::new(&format!("{dest}/{file}")).exists(),
            "{file} missing after install"
        );
    }
    assert!(hermes_plugin_installed(&sys));

    // Idempotent: an installed payload is not rewritten.
    assert!(!install_hermes_plugin(&sys, &bundle, false).unwrap());

    // force refreshes in place.
    assert!(install_hermes_plugin(&sys, &bundle, true).unwrap());

    // A partial install (one file removed, e.g. an older payload) counts as
    // not installed and is repaired without --force.
    std::fs::remove_file(format!("{dest}/__init__.py")).unwrap();
    assert!(!hermes_plugin_installed(&sys));
    assert!(install_hermes_plugin(&sys, &bundle, false).unwrap());
    assert!(hermes_plugin_installed(&sys));
}

#[test]
fn copy_provider_hooks_handles_hermes_provider() {
    let root = tmp_dir("copy");
    let home = root.join("home");
    let bundle = seed_bundle(&root);
    let sys = sys_with_home(&home);

    // The .hermes provider has no JSON hook artifacts: hook_installed is
    // always true (there is no manifest marker to check).
    assert!(hook_installed_for_provider(&home.to_string_lossy(), ".hermes"));

    let written = copy_provider_hooks(&sys, &bundle, &home.to_string_lossy(), &[".hermes"], false, None, None).unwrap();
    assert_eq!(written, vec![".hermes"]);
    assert!(hermes_plugin_installed(&sys));

    // Unknown bundle payloads fail loudly instead of writing a partial plugin.
    let empty = root.join("empty-bundle");
    std::fs::create_dir_all(&empty).unwrap();
    let err = install_hermes_plugin(&sys, &empty.to_string_lossy().as_ref(), true).unwrap_err();
    assert!(err.contains("missing"), "{err}");
}

#[test]
fn hermes_home_env_override_is_honored() {
    let root = tmp_dir("env");
    let home = root.join("home");
    let bundle = seed_bundle(&root);
    let mut env = HashMap::new();
    env.insert("HOME".to_string(), home.to_string_lossy().into_owned());
    // $HERMES_HOME under $HOME must win, like the skills dir resolution.
    let hermes_home = root.join("home/custom-hermes");
    env.insert("HERMES_HOME".to_string(), hermes_home.to_string_lossy().into_owned());
    let sys = Sys::new(env, home.to_string_lossy().into_owned());

    assert!(install_hermes_plugin(&sys, &bundle, false).unwrap());
    let dest = hermes_plugin_dest(&sys);
    assert!(dest.starts_with(hermes_home.to_string_lossy().as_ref()), "{dest}");
    assert!(hermes_plugin_installed(&sys));
}
