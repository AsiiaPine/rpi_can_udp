#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <string>
#include <thread>

#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
#pragma comment(lib, "Ws2_32.lib")
using socket_t = SOCKET;
static constexpr socket_t INVALID_SOCKET_FD = INVALID_SOCKET;
#else
#include <arpa/inet.h>
#include <fcntl.h>
#include <net/if.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <unistd.h>
using socket_t = int;
static constexpr socket_t INVALID_SOCKET_FD = -1;
#endif

#ifdef __linux__
#include <linux/can.h>
#include <linux/can/raw.h>
#endif

namespace {
std::atomic<bool> g_stop_requested{false};

void signal_handler(int) {
    g_stop_requested.store(true, std::memory_order_relaxed);
}

constexpr uint16_t kMagic = 0xCAFE;
constexpr uint8_t kVersion = 1;
constexpr uint8_t kFlagExtended = 1u << 0;
constexpr uint8_t kFlagRtr = 1u << 1;
constexpr uint8_t kFlagError = 1u << 2;

enum class Mode {
    CanToUdp,
    UdpToCan,
    Bridge,
};

struct Config {
    Mode mode = Mode::Bridge;
    std::string can_iface = "can0";
    std::string udp_remote_host = "127.0.0.1";
    uint16_t udp_remote_port = 5000;
    uint16_t udp_listen_port = 5000;
};

#pragma pack(push, 1)
struct UdpCanFrame {
    uint16_t magic;
    uint8_t version;
    uint8_t flags;
    uint32_t can_id;
    uint8_t dlc;
    uint8_t data[8];
    uint32_t seq;
    uint32_t crc32;
};
#pragma pack(pop)

uint32_t crc32_ieee(const uint8_t* data, size_t size) {
    static uint32_t table[256];
    static bool table_ready = false;
    if (!table_ready) {
        for (uint32_t i = 0; i < 256; ++i) {
            uint32_t c = i;
            for (int j = 0; j < 8; ++j) {
                c = (c & 1u) ? (0xEDB88320u ^ (c >> 1u)) : (c >> 1u);
            }
            table[i] = c;
        }
        table_ready = true;
    }

    uint32_t c = 0xFFFFFFFFu;
    for (size_t i = 0; i < size; ++i) {
        c = table[(c ^ data[i]) & 0xFFu] ^ (c >> 8u);
    }
    return c ^ 0xFFFFFFFFu;
}

void close_socket(socket_t s) {
    if (s == INVALID_SOCKET_FD) {
        return;
    }
#ifdef _WIN32
    closesocket(s);
#else
    close(s);
#endif
}

bool parse_mode(const std::string& in, Mode& mode) {
    if (in == "can2udp") {
        mode = Mode::CanToUdp;
        return true;
    }
    if (in == "udp2can") {
        mode = Mode::UdpToCan;
        return true;
    }
    if (in == "bridge") {
        mode = Mode::Bridge;
        return true;
    }
    return false;
}

bool parse_args(int argc, char** argv, Config& cfg) {
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        auto need_value = [&](const char* name) -> const char* {
            if (i + 1 >= argc) {
                std::cerr << "Missing value for " << name << "\n";
                return nullptr;
            }
            return argv[++i];
        };

        if (arg == "--mode") {
            const char* value = need_value("--mode");
            if (!value || !parse_mode(value, cfg.mode)) {
                std::cerr << "Invalid --mode. Use can2udp|udp2can|bridge\n";
                return false;
            }
        } else if (arg == "--can-iface") {
            const char* value = need_value("--can-iface");
            if (!value) return false;
            cfg.can_iface = value;
        } else if (arg == "--udp-remote-host") {
            const char* value = need_value("--udp-remote-host");
            if (!value) return false;
            cfg.udp_remote_host = value;
        } else if (arg == "--udp-remote-port") {
            const char* value = need_value("--udp-remote-port");
            if (!value) return false;
            cfg.udp_remote_port = static_cast<uint16_t>(std::stoi(value));
        } else if (arg == "--udp-listen-port") {
            const char* value = need_value("--udp-listen-port");
            if (!value) return false;
            cfg.udp_listen_port = static_cast<uint16_t>(std::stoi(value));
        } else if (arg == "--help" || arg == "-h") {
            std::cout
                << "Usage: can_udp_bridge [options]\n"
                << "  --mode can2udp|udp2can|bridge (default: bridge)\n"
                << "  --can-iface can0|can1...        (default: can0)\n"
                << "  --udp-remote-host <ip/host>     (default: 127.0.0.1)\n"
                << "  --udp-remote-port <port>        (default: 5000)\n"
                << "  --udp-listen-port <port>        (default: 5000)\n";
            return false;
        } else {
            std::cerr << "Unknown argument: " << arg << "\n";
            return false;
        }
    }
    return true;
}

class UdpEndpoint {
public:
    ~UdpEndpoint() {
        close();
    }

    bool open_receiver(uint16_t listen_port) {
        rx_ = socket(AF_INET, SOCK_DGRAM, 0);
        if (rx_ == INVALID_SOCKET_FD) {
            std::cerr << "Failed to create UDP receive socket\n";
            return false;
        }

        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_addr.s_addr = htonl(INADDR_ANY);
        addr.sin_port = htons(listen_port);

        if (bind(rx_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0) {
            std::cerr << "Failed to bind UDP receive socket on port " << listen_port << "\n";
            return false;
        }
#ifdef _WIN32
        u_long mode = 1;
        if (ioctlsocket(rx_, FIONBIO, &mode) != 0) {
            std::cerr << "Failed to set UDP receive socket non-blocking\n";
            return false;
        }
#else
        const int flags = fcntl(rx_, F_GETFL, 0);
        if (flags < 0 || fcntl(rx_, F_SETFL, flags | O_NONBLOCK) < 0) {
            std::cerr << "Failed to set UDP receive socket non-blocking\n";
            return false;
        }
#endif
        return true;
    }

    bool open_sender(const std::string& host, uint16_t port) {
        tx_ = socket(AF_INET, SOCK_DGRAM, 0);
        if (tx_ == INVALID_SOCKET_FD) {
            std::cerr << "Failed to create UDP send socket\n";
            return false;
        }

        std::memset(&remote_, 0, sizeof(remote_));
        remote_.sin_family = AF_INET;
        remote_.sin_port = htons(port);

        if (inet_pton(AF_INET, host.c_str(), &remote_.sin_addr) != 1) {
            std::cerr << "Invalid UDP remote host: " << host << "\n";
            return false;
        }
        return true;
    }

    bool send_frame(const UdpCanFrame& frame) {
        const auto sent = sendto(
            tx_,
            reinterpret_cast<const char*>(&frame),
            sizeof(frame),
            0,
            reinterpret_cast<sockaddr*>(&remote_),
            sizeof(remote_)
        );
        return sent == static_cast<int>(sizeof(frame));
    }

    bool recv_frame(UdpCanFrame& frame) {
        sockaddr_in src{};
        socklen_t src_len = sizeof(src);
        const auto got = recvfrom(
            rx_,
            reinterpret_cast<char*>(&frame),
            sizeof(frame),
            0,
            reinterpret_cast<sockaddr*>(&src),
            &src_len
        );
        if (got < 0) {
#ifdef _WIN32
            const int err = WSAGetLastError();
            if (err == WSAEWOULDBLOCK) {
                return false;
            }
#else
            if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) {
                return false;
            }
#endif
            return false;
        }
        return got == static_cast<int>(sizeof(frame));
    }

    void close() {
        close_socket(rx_);
        close_socket(tx_);
        rx_ = INVALID_SOCKET_FD;
        tx_ = INVALID_SOCKET_FD;
    }

private:
    socket_t rx_ = INVALID_SOCKET_FD;
    socket_t tx_ = INVALID_SOCKET_FD;
    sockaddr_in remote_{};
};

class CanEndpoint {
public:
    ~CanEndpoint() {
        close();
    }

    bool open(const std::string& iface) {
#ifdef __linux__
        fd_ = socket(PF_CAN, SOCK_RAW, CAN_RAW);
        if (fd_ < 0) {
            std::cerr << "Failed to create CAN socket\n";
            return false;
        }

        ifreq ifr{};
        std::strncpy(ifr.ifr_name, iface.c_str(), IFNAMSIZ - 1);
        if (ioctl(fd_, SIOCGIFINDEX, &ifr) < 0) {
            std::cerr << "Unknown CAN interface: " << iface << "\n";
            return false;
        }

        sockaddr_can addr{};
        addr.can_family = AF_CAN;
        addr.can_ifindex = ifr.ifr_ifindex;
        if (bind(fd_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0) {
            std::cerr << "Failed to bind CAN interface: " << iface << "\n";
            return false;
        }
        const int flags = fcntl(fd_, F_GETFL, 0);
        if (flags < 0 || fcntl(fd_, F_SETFL, flags | O_NONBLOCK) < 0) {
            std::cerr << "Failed to set CAN socket non-blocking\n";
            return false;
        }
        return true;
#else
        (void)iface;
        std::cerr << "CAN backend is implemented only for Linux in this build.\n";
        return false;
#endif
    }

    bool recv_can_frame(uint32_t& can_id_raw, uint8_t& dlc, uint8_t data[8]) {
#ifdef __linux__
        can_frame frame{};
        const auto got = read(fd_, &frame, sizeof(frame));
        if (got < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) {
                return false;
            }
            return false;
        }
        if (got != static_cast<ssize_t>(sizeof(frame))) {
            return false;
        }
        can_id_raw = frame.can_id;
        dlc = frame.can_dlc;
        std::memcpy(data, frame.data, 8);
        return true;
#else
        (void)can_id_raw;
        (void)dlc;
        (void)data;
        return false;
#endif
    }

    bool send_can_frame(uint32_t can_id_raw, uint8_t dlc, const uint8_t data[8]) {
#ifdef __linux__
        can_frame frame{};
        frame.can_id = can_id_raw;
        frame.can_dlc = dlc;
        std::memcpy(frame.data, data, 8);
        const auto sent = write(fd_, &frame, sizeof(frame));
        return sent == static_cast<ssize_t>(sizeof(frame));
#else
        (void)can_id_raw;
        (void)dlc;
        (void)data;
        return false;
#endif
    }

    void close() {
#ifdef __linux__
        if (fd_ >= 0) {
            ::close(fd_);
            fd_ = -1;
        }
#endif
    }

private:
#ifdef __linux__
    int fd_ = -1;
#endif
};

void fill_udp_from_can(UdpCanFrame& out, uint32_t can_id_raw, uint8_t dlc, const uint8_t data[8], uint32_t seq) {
    std::memset(&out, 0, sizeof(out));
    out.magic = htons(kMagic);
    out.version = kVersion;
    out.flags = 0;

#ifdef __linux__
    if (can_id_raw & CAN_EFF_FLAG) out.flags |= kFlagExtended;
    if (can_id_raw & CAN_RTR_FLAG) out.flags |= kFlagRtr;
    if (can_id_raw & CAN_ERR_FLAG) out.flags |= kFlagError;
    const uint32_t can_id = (can_id_raw & CAN_EFF_FLAG) ? (can_id_raw & CAN_EFF_MASK)
                                                         : (can_id_raw & CAN_SFF_MASK);
#else
    const uint32_t can_id = can_id_raw;
#endif

    out.can_id = htonl(can_id);
    out.dlc = dlc > 8 ? 8 : dlc;
    std::memcpy(out.data, data, 8);
    out.seq = htonl(seq);
    out.crc32 = 0;
    const uint32_t crc = crc32_ieee(reinterpret_cast<const uint8_t*>(&out), sizeof(out) - sizeof(out.crc32));
    out.crc32 = htonl(crc);
}

bool parse_udp_to_can(const UdpCanFrame& in, uint32_t& can_id_raw, uint8_t& dlc, uint8_t data[8]) {
    if (ntohs(in.magic) != kMagic || in.version != kVersion) {
        return false;
    }
    const uint32_t expected_crc = ntohl(in.crc32);
    UdpCanFrame tmp = in;
    tmp.crc32 = 0;
    const uint32_t calc_crc = crc32_ieee(reinterpret_cast<const uint8_t*>(&tmp), sizeof(tmp) - sizeof(tmp.crc32));
    if (expected_crc != calc_crc) {
        return false;
    }

    const uint32_t can_id = ntohl(in.can_id);
    dlc = in.dlc > 8 ? 8 : in.dlc;
    std::memcpy(data, in.data, 8);

#ifdef __linux__
    if (in.flags & kFlagExtended) {
        can_id_raw = (can_id & CAN_EFF_MASK) | CAN_EFF_FLAG;
    } else {
        can_id_raw = (can_id & CAN_SFF_MASK);
    }
    if (in.flags & kFlagRtr) can_id_raw |= CAN_RTR_FLAG;
    if (in.flags & kFlagError) can_id_raw |= CAN_ERR_FLAG;
#else
    can_id_raw = can_id;
#endif
    return true;
}

}  // namespace

int main(int argc, char** argv) {
#ifdef _WIN32
    WSADATA wsa_data{};
    if (WSAStartup(MAKEWORD(2, 2), &wsa_data) != 0) {
        std::cerr << "WSAStartup failed\n";
        return 1;
    }
#endif

    Config cfg;
    if (!parse_args(argc, argv, cfg)) {
#ifdef _WIN32
        WSACleanup();
#endif
        return 1;
    }

    CanEndpoint can;
    UdpEndpoint udp;

    if (cfg.mode == Mode::CanToUdp || cfg.mode == Mode::Bridge || cfg.mode == Mode::UdpToCan) {
        if (!can.open(cfg.can_iface)) {
#ifdef _WIN32
            WSACleanup();
#endif
            return 1;
        }
    }
    if (cfg.mode == Mode::CanToUdp || cfg.mode == Mode::Bridge) {
        if (!udp.open_sender(cfg.udp_remote_host, cfg.udp_remote_port)) {
#ifdef _WIN32
            WSACleanup();
#endif
            return 1;
        }
    }
    if (cfg.mode == Mode::UdpToCan || cfg.mode == Mode::Bridge) {
        if (!udp.open_receiver(cfg.udp_listen_port)) {
#ifdef _WIN32
            WSACleanup();
#endif
            return 1;
        }
    }

    std::atomic<bool> running{true};
    std::atomic<uint64_t> rx_ok{0};
    std::atomic<uint64_t> tx_ok{0};
    std::atomic<uint64_t> udp_rx_frames{0};
    std::atomic<uint64_t> can_tx_fail{0};
    std::atomic<uint64_t> crc_drop{0};
    std::atomic<uint32_t> seq{0};

    std::thread t_can_to_udp;
    std::thread t_udp_to_can;

    if (cfg.mode == Mode::CanToUdp || cfg.mode == Mode::Bridge) {
        t_can_to_udp = std::thread([&]() {
            while (running.load(std::memory_order_relaxed) && !g_stop_requested.load(std::memory_order_relaxed)) {
                uint32_t can_id_raw = 0;
                uint8_t dlc = 0;
                uint8_t data[8]{};
                if (!can.recv_can_frame(can_id_raw, dlc, data)) {
                    std::this_thread::sleep_for(std::chrono::milliseconds(1));
                    continue;
                }
                UdpCanFrame frame{};
                fill_udp_from_can(frame, can_id_raw, dlc, data, seq.fetch_add(1, std::memory_order_relaxed));
                if (udp.send_frame(frame)) {
                    tx_ok.fetch_add(1, std::memory_order_relaxed);
                }
            }
        });
    }

    if (cfg.mode == Mode::UdpToCan || cfg.mode == Mode::Bridge) {
        t_udp_to_can = std::thread([&]() {
            while (running.load(std::memory_order_relaxed) && !g_stop_requested.load(std::memory_order_relaxed)) {
                UdpCanFrame frame{};
                if (!udp.recv_frame(frame)) {
                    std::this_thread::sleep_for(std::chrono::milliseconds(1));
                    continue;
                }
                udp_rx_frames.fetch_add(1, std::memory_order_relaxed);
                uint32_t can_id_raw = 0;
                uint8_t dlc = 0;
                uint8_t data[8]{};
                if (!parse_udp_to_can(frame, can_id_raw, dlc, data)) {
                    crc_drop.fetch_add(1, std::memory_order_relaxed);
                    continue;
                }
                if (can.send_can_frame(can_id_raw, dlc, data)) {
                    rx_ok.fetch_add(1, std::memory_order_relaxed);
                } else {
                    const auto fails = can_tx_fail.fetch_add(1, std::memory_order_relaxed) + 1;
                    if (fails <= 5) {
                        std::cerr << "[WARN] CAN write failed, errno=" << errno
                                  << " (" << std::strerror(errno) << ")\n";
                    }
                }
            }
        });
    }

    std::cout << "Bridge started. mode="
              << (cfg.mode == Mode::CanToUdp ? "can2udp" : cfg.mode == Mode::UdpToCan ? "udp2can" : "bridge")
              << ", can=" << cfg.can_iface
              << ", udp_remote=" << cfg.udp_remote_host << ":" << cfg.udp_remote_port
              << ", udp_listen=" << cfg.udp_listen_port << "\n";
    std::cout << "Press Ctrl+C to stop.\n";

    // Simple live stats loop.
    std::signal(SIGINT, signal_handler);
    std::signal(SIGTERM, signal_handler);
    while (!g_stop_requested.load(std::memory_order_relaxed)) {
        std::this_thread::sleep_for(std::chrono::seconds(2));
        std::cout << "tx_can2udp=" << tx_ok.load(std::memory_order_relaxed)
                  << " udp_rx_frames=" << udp_rx_frames.load(std::memory_order_relaxed)
                  << " rx_udp2can=" << rx_ok.load(std::memory_order_relaxed)
                  << " can_tx_fail=" << can_tx_fail.load(std::memory_order_relaxed)
                  << " dropped_bad_crc=" << crc_drop.load(std::memory_order_relaxed)
                  << "\n";
    }

    running.store(false, std::memory_order_relaxed);
    g_stop_requested.store(true, std::memory_order_relaxed);
    udp.close();
    can.close();
    if (t_can_to_udp.joinable()) t_can_to_udp.join();
    if (t_udp_to_can.joinable()) t_udp_to_can.join();

#ifdef _WIN32
    WSACleanup();
#endif
    return 0;
}
