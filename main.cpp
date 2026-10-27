#include <gtk/gtk.h>
#include <webkit2/webkit2.h>

#include <arpa/inet.h>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <iostream>
#include <sstream>
#include <string>
#include <sys/socket.h>
#include <sys/wait.h>
#include <thread>
#include <unistd.h>

namespace {

pid_t g_backend_pid = -1;

struct HostConfig {
    std::string host = "127.0.0.1";
    int port = 8470;
    int width = 1360;
    int height = 860;
    std::string title = "Raven Nest";
    std::string subtitle = "Windows Application Sandbox & Deployment Manager";
    std::string theme = "dark";
};

// Reads resolved configuration from ConfigService ("Everything as Config")
HostConfig load_host_config(const std::filesystem::path &ui_dir) {
    HostConfig cfg;
    std::string cmd = "python3 -c \"import sys; sys.path.insert(0, '" + ui_dir.string() +
                      "'); from services.config_service import ConfigService; from pathlib import Path; "
                      "c = ConfigService(Path('" + ui_dir.string() + "')).data; "
                      "print(c['server']['host']); print(c['server']['port']); "
                      "print(c['ui']['window_width']); print(c['ui']['window_height']); "
                      "print(c['ui']['title']); print(c['ui']['subtitle']); "
                      "print(c['ui']['default_theme'])\" 2>/dev/null";
    FILE *pipe = ::popen(cmd.c_str(), "r");
    if (pipe) {
        char line[512];
        auto read_line = [&]() -> std::string {
            if (!std::fgets(line, sizeof(line), pipe)) return "";
            std::string s(line);
            while (!s.empty() && (s.back() == '\n' || s.back() == '\r')) s.pop_back();
            return s;
        };
        if (auto v = read_line(); !v.empty()) cfg.host = v;
        if (auto v = read_line(); !v.empty()) cfg.port = std::atoi(v.c_str());
        if (auto v = read_line(); !v.empty()) cfg.width = std::atoi(v.c_str());
        if (auto v = read_line(); !v.empty()) cfg.height = std::atoi(v.c_str());
        if (auto v = read_line(); !v.empty()) cfg.title = v;
        if (auto v = read_line(); !v.empty()) cfg.subtitle = v;
        if (auto v = read_line(); !v.empty()) cfg.theme = v;
        ::pclose(pipe);
    }
    if (const char *env_port = std::getenv("RAVEN_UI_PORT")) {
        cfg.port = std::atoi(env_port);
    }
    return cfg;
}

bool is_port_open(const std::string &host, int port) {
    int sock = ::socket(AF_INET, SOCK_STREAM, 0);
    if (sock < 0) return false;
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<uint16_t>(port));
    ::inet_pton(AF_INET, host.c_str(), &addr.sin_addr);
    bool ok = (::connect(sock, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) == 0);
    ::close(sock);
    return ok;
}

void stop_backend() {
    if (g_backend_pid > 0) {
        ::kill(g_backend_pid, SIGTERM);
        int status = 0;
        ::waitpid(g_backend_pid, &status, 0);
        g_backend_pid = -1;
    }
}

bool ensure_backend_running(const std::filesystem::path &ui_dir, const HostConfig &cfg) {
    if (is_port_open(cfg.host, cfg.port)) {
        return true;
    }
    const auto backend_script = ui_dir / "backend.py";
    pid_t pid = ::fork();
    if (pid == 0) {
        std::string port_str = std::to_string(cfg.port);
        ::setenv("RAVEN_UI_HOST", cfg.host.c_str(), 1);
        ::setenv("RAVEN_UI_PORT", port_str.c_str(), 1);
        ::execlp("python3", "python3", backend_script.c_str(), static_cast<char *>(nullptr));
        ::_exit(127);
    }
    if (pid < 0) {
        return false;
    }
    g_backend_pid = pid;
    for (int i = 0; i < 50; ++i) {
        if (is_port_open(cfg.host, cfg.port)) {
            return true;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(60));
    }
    return is_port_open(cfg.host, cfg.port);
}

void on_window_destroy(GtkWidget *, gpointer) {
    stop_backend();
    gtk_main_quit();
}

} // namespace

int main(int argc, char **argv) {
    const auto exe_path = std::filesystem::read_symlink("/proc/self/exe");
    const auto ui_dir = exe_path.parent_path();
    const HostConfig cfg = load_host_config(ui_dir);
    const std::string url = "http://" + cfg.host + ":" + std::to_string(cfg.port);

    bool web_only = false;
    for (int i = 1; i < argc; ++i) {
        std::string_view arg(argv[i]);
        if (arg == "--web" || arg == "--headless") {
            web_only = true;
        }
    }

    if (!ensure_backend_running(ui_dir, cfg)) {
        std::cerr << "raven-ui: failed to start backend daemon at " << url << "\n";
        return 1;
    }
    std::atexit(stop_backend);

    if (web_only || !gtk_init_check(&argc, &argv)) {
        std::cout << cfg.title << " Web UI running at " << url << "\n";
        if (g_backend_pid > 0) {
            int status = 0;
            ::waitpid(g_backend_pid, &status, 0);
        }
        return 0;
    }

    GtkSettings *settings = gtk_settings_get_default();
    if (settings) {
        g_object_set(settings, "gtk-application-prefer-dark-theme", cfg.theme != "light" ? TRUE : FALSE, nullptr);
    }

    GtkWidget *window = gtk_window_new(GTK_WINDOW_TOPLEVEL);
    gtk_window_set_title(GTK_WINDOW(window), cfg.title.c_str());
    gtk_window_set_default_size(GTK_WINDOW(window), cfg.width, cfg.height);
    gtk_window_set_position(GTK_WINDOW(window), GTK_WIN_POS_CENTER);

    GtkWidget *header = gtk_header_bar_new();
    gtk_header_bar_set_show_close_button(GTK_HEADER_BAR(header), TRUE);
    gtk_header_bar_set_title(GTK_HEADER_BAR(header), cfg.title.c_str());
    gtk_header_bar_set_subtitle(GTK_HEADER_BAR(header), cfg.subtitle.c_str());
    gtk_window_set_titlebar(GTK_WINDOW(window), header);

    WebKitWebContext *context = webkit_web_context_get_default();
    webkit_web_context_set_cache_model(context, WEBKIT_CACHE_MODEL_DOCUMENT_VIEWER);

    GtkWidget *webview = webkit_web_view_new_with_context(context);
    WebKitSettings *wk_settings = webkit_web_view_get_settings(WEBKIT_WEB_VIEW(webview));
    webkit_settings_set_enable_developer_extras(wk_settings, TRUE);
    webkit_settings_set_enable_webgl(wk_settings, TRUE);

    GdkRGBA bg_color = cfg.theme == "light" ? GdkRGBA{0.96, 0.96, 0.98, 1.0} : GdkRGBA{0.11, 0.11, 0.13, 1.0};
    webkit_web_view_set_background_color(WEBKIT_WEB_VIEW(webview), &bg_color);

    gtk_container_add(GTK_CONTAINER(window), webview);
    webkit_web_view_load_uri(WEBKIT_WEB_VIEW(webview), url.c_str());

    g_signal_connect(window, "destroy", G_CALLBACK(on_window_destroy), nullptr);
    gtk_widget_show_all(window);

    gtk_main();
    return 0;
}
