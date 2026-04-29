import os from "node:os";

const originalNetworkInterfaces = os.networkInterfaces;

// In some restricted environments (e.g., sandboxed containers), Node's
// `os.networkInterfaces()` can throw due to `uv_interface_addresses` failures.
// OpenClaw calls this during CLI startup to pick a LAN IP. Falling back to an
// empty map is sufficient (it will then fall back to hostname / localhost).
os.networkInterfaces = () => {
  try {
    return originalNetworkInterfaces();
  } catch {
    // Provide a minimal loopback interface map so OpenClaw can still resolve
    // bind=loopback correctly (127.0.0.1 / ::1) instead of falling back to 0.0.0.0.
    return {
      lo: [
        {
          address: "127.0.0.1",
          netmask: "255.0.0.0",
          family: "IPv4",
          mac: "00:00:00:00:00:00",
          internal: true,
          cidr: "127.0.0.1/8"
        },
        {
          address: "::1",
          netmask: "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
          family: "IPv6",
          mac: "00:00:00:00:00:00",
          internal: true,
          cidr: "::1/128"
        }
      ]
    };
  }
};

