#!/bin/bash

# Add the selected runtime's regional payload to the ISO. Repository defaults
# stay in the runtime package, not in a second set of ISO-specific templates.
set -euo pipefail

region="$1"
runtime="$2"
iso_root="$3"
online_config="$4"

case "$region" in
  global) exit 0 ;;
  cn) ;;
  *) echo "Unsupported region: $region" >&2; exit 1 ;;
esac

profile="$runtime/default/regions/$region"
if [[ ! -f $runtime/bin/omarchy-apply-pacman || ! -f $profile/packages ||
      ! -f $profile/pacman/pacman.conf.append || ! -f $profile/pacman/mirrorlist.append ||
      ! -d $profile/skel ]]; then
  echo "Runtime does not support region '$region'; publish a matching runtime or use --local-source." >&2
  exit 1
fi

# This is the target package list used both for download resolution and by the
# installer. Merely putting extra packages in the cache would not install them.
payload="$iso_root/usr/share/omarchy-iso"
cp -a "$profile" "$payload/region"
printf '\n' >> "$payload/omarchy-base.packages"
cat "$profile/packages" >> "$payload/omarchy-base.packages"

# Only the community repository is needed for regional package downloads.
# Keep the channel's build mirrors intact; target mirrors are restored later.
printf '\n' >> "$online_config"
cat "$profile/pacman/pacman.conf.append" >> "$online_config"

# The keyring package must be signed by a key already trusted by Arch. Do not
# bootstrap trust from an unchecked keyserver response or disable signatures.
# https://github.com/archlinuxcn/repo#usage
pacman --config "$online_config" --noconfirm -Sy --needed archlinuxcn-keyring
pacman-key --populate archlinuxcn
