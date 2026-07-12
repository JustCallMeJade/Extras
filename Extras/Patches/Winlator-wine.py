import os
import sys

# Define the root directory to apply the patch (defaults to the current working directory)
SOURCE_DIR = "."

# Dictionary containing the files to patch and their respective find/replace pairs
PATCHES = {
    "dlls/ntdll/unix/server.c": [
        (
            'asprintf( &dir, "/tmp/.wine-%u/server-%llx-%llx", getuid(), (unsigned long long)dev, (unsigned long long)ino );',
            'asprintf( &dir, "/data/data/com.winlator/files/rootfs/tmp/.wine-%u/server-%llx-%llx", getuid(), (unsigned long long)dev, (unsigned long long)ino );'
        ),
        (
            'symlink( "/", "dosdevices/z:" );',
            'symlink( "/data/data/com.winlator/files/rootfs", "dosdevices/z:" );'
        )
    ],
    "server/request.c": [
        (
            'if (asprintf( &base_dir, "/tmp/.wine-%u", getuid() ) == -1)',
            'if (asprintf( &base_dir, "/data/data/com.winlator/files/rootfs/tmp/.wine-%u", getuid() ) == -1)'
        )
    ],
    "server/unicode.c": [
        (
            'static const char *nls_dirs[] = { NULL, DATADIR "/wine/nls", "/usr/local/share/wine/nls", "/usr/share/wine/nls" };',
            'static const char *nls_dirs[] = { NULL, DATADIR "/wine/nls", "/data/data/com.winlator/files/rootfs/usr/local/share/wine/nls", "/data/data/com.winlator/files/rootfs/usr/share/wine/nls" };'
        )
    ]
}

def apply_smart_patch():
    print(f"Starting Smart Patcher for Winlator Rootfs in '{os.path.abspath(SOURCE_DIR)}'...\n")
    
    for filepath, replacements in PATCHES.items():
        full_path = os.path.join(SOURCE_DIR, filepath)

        if not os.path.exists(full_path):
            print(f"[-] Warning: File not found: {full_path}")
            continue

        # Read the file
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()

        original_content = content
        
        # Apply the replacements
        for search_text, replace_text in replacements:
            if search_text in content:
                content = content.replace(search_text, replace_text)
                print(f"[+] Successfully patched target line in {filepath}")
            elif replace_text in content:
                print(f"[~] Target line in {filepath} is already patched.")
            else:
                print(f"[-] Error: Could not find the target search string in {filepath}. The source code may have changed significantly.")

        # Write changes if modifications were made
        if content != original_content:
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(content)
            print(f"    -> Saved changes to {filepath}\n")
        else:
            print(f"    -> No new changes made to {filepath}\n")

if __name__ == "__main__":
    apply_smart_patch()
    print("Patching complete.")
