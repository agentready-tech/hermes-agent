# Chromium seccomp policy

`chromium-seccomp.json` derives from Moby's default seccomp profile at commit
`6fe7deb1b9fb7c0397a4593480d7d22b9ee8caef` (upstream file blob
`6e5e47e242892aaa290f916ba1045f9213ef0b84`):
https://github.com/moby/profiles/blob/6fe7deb1b9fb7c0397a4593480d7d22b9ee8caef/seccomp/default.json

Modification: an unconditional allow rule for `clone`, `setns`, `unshare`, and `chroot`.
Chromium uses these to build its nested user/PID/network namespace sandbox. In particular,
Hermes drops `CAP_SYS_CHROOT`, so Moby's capability-gated `chroot` rule is insufficient
even after Chromium has entered its own user namespace. All remaining rules and the
default deny action are unchanged. The upstream Apache 2.0 license is included here.

This is an intentional relaxation of Docker's syscall filter, not proof against container
escape. AppArmor, no-new-privileges, capability dropping, resource limits and separate
namespaces remain in force. Neither `NET_ADMIN` nor a TUN device is enabled by this policy.
Host restrictions on unprivileged user namespaces can still prevent Chromium from starting.
