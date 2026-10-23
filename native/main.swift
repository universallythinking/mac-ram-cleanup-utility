import Cocoa
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, NSWindowDelegate, WKScriptMessageHandler {
    var fullWindow: NSWindow?
    var miniWindow: NSWindow?
    var webView: WKWebView?
    var server: Process?
    var statusItem: NSStatusItem?

    private var usedLabel: NSTextField!
    private var detailLabel: NSTextField!
    private var pollTimer: Timer?
    private var cmdTimer: Timer?
    /// True only while the mini widget is the visible UI (not a launch preference).
    private var showingMini = false

    let port = 8765
    let host = "127.0.0.1"
    private let defaults = UserDefaults.standard
    private let kFullFrame = "fullWindowFrame"
    private let kMiniFrame = "miniWindowFrame"

    private var cmdMiniURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".ramcleanup")
            .appendingPathComponent("cmd_mini")
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        installStatusItem()
        // Clear any stale mini request from a previous session.
        try? FileManager.default.removeItem(at: cmdMiniURL)

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            self?.startServerIfNeeded()
        }

        // Always open the full window on launch (never boot into mini).
        showFull(activate: true)
        startPolling()
        startCommandPoll()
        NSApp.activate(ignoringOtherApps: true)
        appendLog("launch → full window")
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if showingMini {
            showMini(activate: true)
        } else {
            showFull(activate: true)
        }
        NSApp.activate(ignoringOtherApps: true)
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        // We manage lifetime explicitly (mini can be the only window).
        false
    }

    func applicationWillTerminate(_ notification: Notification) {
        pollTimer?.invalidate()
        cmdTimer?.invalidate()
        webView?.configuration.userContentController.removeScriptMessageHandler(forName: "ramcleanup")
        saveFrames()
        stopServer()
    }

    // MARK: - Mini command channel (web UI → native)

    private func startCommandPoll() {
        cmdTimer?.invalidate()
        cmdTimer = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] _ in
            self?.consumeMiniCommand()
        }
        if let cmdTimer {
            RunLoop.main.add(cmdTimer, forMode: .common)
        }
    }

    private func consumeMiniCommand() {
        let url = cmdMiniURL
        guard FileManager.default.fileExists(atPath: url.path) else { return }
        try? FileManager.default.removeItem(at: url)
        appendLog("cmd_mini received → showMini")
        showMini(activate: true)
    }

    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage
    ) {
        guard message.name == "ramcleanup" else { return }
        let body = (message.body as? String) ?? "\(message.body)"
        appendLog("webkit message: \(body)")
        if body == "mini" {
            DispatchQueue.main.async { [weak self] in
                self?.showMini(activate: true)
            }
        } else if body == "full" {
            DispatchQueue.main.async { [weak self] in
                self?.showFull(activate: true)
            }
        }
    }

    // MARK: - Menu bar

    private func installStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "RAM"
        item.button?.toolTip = "RAM Cleanup"
        let menu = NSMenu()
        menu.addItem(NSMenuItem(title: "Open Full Window", action: #selector(menuShowFull), keyEquivalent: "o"))
        menu.addItem(NSMenuItem(title: "Show Mini (always on top)", action: #selector(menuShowMini), keyEquivalent: "m"))
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "Quit RAM Cleanup", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        item.menu = menu
        statusItem = item
    }

    @objc private func menuShowMini() { showMini(activate: true) }
    @objc private func menuShowFull() { showFull(activate: true) }

    // MARK: - Frames

    private func saveFrames() {
        if let full = fullWindow, full.isVisible {
            defaults.set(NSStringFromRect(full.frame), forKey: kFullFrame)
        }
        if let mini = miniWindow, mini.isVisible {
            defaults.set(NSStringFromRect(mini.frame), forKey: kMiniFrame)
        }
    }

    private func screenForUI() -> NSScreen {
        if let s = fullWindow?.screen { return s }
        if let s = NSScreen.main { return s }
        if let s = NSScreen.screens.first { return s }
        // Extremely unlikely; keeps the type non-optional.
        return NSScreen.screens[0]
    }

    private func defaultMiniFrame(on screen: NSScreen) -> NSRect {
        let vis = screen.visibleFrame
        let w: CGFloat = 230
        let h: CGFloat = 100
        let margin: CGFloat = 24
        return NSRect(
            x: vis.maxX - w - margin,
            y: vis.maxY - h - margin,
            width: w,
            height: h
        )
    }

    private func restoredFrame(key: String, fallback: NSRect) -> NSRect {
        guard let s = defaults.string(forKey: key) else { return fallback }
        let r = NSRectFromString(s)
        guard r.width >= 80, r.height >= 40 else { return fallback }
        let onScreen = NSScreen.screens.contains { $0.frame.intersects(r) }
        return onScreen ? r : fallback
    }

    // MARK: - Mini (always on top)

    private func ensureMiniWindow() -> NSWindow {
        if let existing = miniWindow {
            return existing
        }

        let screen = screenForUI()
        let frame = defaultMiniFrame(on: screen)

        // NSPanel is the reliable always-on-top utility window type.
        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: frame.width, height: frame.height),
            styleMask: [.titled, .closable, .utilityWindow],
            backing: .buffered,
            defer: false
        )
        panel.title = "RAM"
        panel.isFloatingPanel = true
        panel.becomesKeyOnlyIfNeeded = false
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.isMovable = true
        panel.isMovableByWindowBackground = true
        panel.hasShadow = true
        panel.delegate = self
        panel.backgroundColor = NSColor(calibratedRed: 0.06, green: 0.40, blue: 0.38, alpha: 1)

        // Manual layout (no Auto Layout) — avoids constraint exceptions.
        let root = NSView(frame: NSRect(x: 0, y: 0, width: frame.width, height: frame.height - 28))
        root.wantsLayer = true
        root.layer?.backgroundColor = NSColor(calibratedRed: 0.06, green: 0.40, blue: 0.38, alpha: 1).cgColor
        root.autoresizingMask = [.width, .height]

        let used = NSTextField(frame: NSRect(x: 8, y: 36, width: frame.width - 16, height: 32))
        used.stringValue = "—"
        used.font = NSFont.monospacedDigitSystemFont(ofSize: 26, weight: .bold)
        used.textColor = .white
        used.alignment = .center
        used.isBezeled = false
        used.drawsBackground = false
        used.isEditable = false
        used.isSelectable = false
        used.autoresizingMask = [.width]
        usedLabel = used

        let detail = NSTextField(frame: NSRect(x: 8, y: 12, width: frame.width - 90, height: 18))
        detail.stringValue = "memory used"
        detail.font = NSFont.systemFont(ofSize: 11, weight: .medium)
        detail.textColor = NSColor.white.withAlphaComponent(0.9)
        detail.isBezeled = false
        detail.drawsBackground = false
        detail.isEditable = false
        detail.isSelectable = false
        detail.autoresizingMask = [.width]
        detailLabel = detail

        let openBtn = NSButton(frame: NSRect(x: frame.width - 78, y: 8, width: 66, height: 24))
        openBtn.title = "Open"
        openBtn.bezelStyle = .rounded
        openBtn.font = NSFont.systemFont(ofSize: 11, weight: .semibold)
        openBtn.target = self
        openBtn.action = #selector(expandToFull)
        openBtn.autoresizingMask = [.minXMargin]

        root.addSubview(used)
        root.addSubview(detail)
        root.addSubview(openBtn)
        panel.contentView = root
        panel.setFrame(frame, display: false)

        miniWindow = panel
        appendLog("mini panel created at \(NSStringFromRect(frame))")
        return panel
    }

    private func showMini(activate: Bool) {
        appendLog("showMini step1")
        let mini = ensureMiniWindow()
        appendLog("showMini step2")

        let screen = screenForUI()
        let frame = defaultMiniFrame(on: screen)
        mini.setFrame(frame, display: true)
        appendLog("showMini step3 frame=\(NSStringFromRect(frame))")

        mini.level = .floating
        mini.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        mini.alphaValue = 1

        showingMini = true
        mini.orderFrontRegardless()
        appendLog("showMini step4 ordered front visible=\(mini.isVisible)")

        fullWindow?.orderOut(nil)
        appendLog("showMini step5 full ordered out")

        mini.makeKeyAndOrderFront(nil)
        if activate {
            NSApp.activate(ignoringOtherApps: true)
        }

        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [weak self] in
            guard let self, self.showingMini, let mini = self.miniWindow else { return }
            self.fullWindow?.orderOut(nil)
            mini.level = .floating
            mini.orderFrontRegardless()
            mini.makeKeyAndOrderFront(nil)
            self.appendLog("showMini step6 reassert visible=\(mini.isVisible)")
        }

        refreshMiniStats()
        appendLog("showMini done")
    }

    @objc private func expandToFull() {
        showFull(activate: true)
    }

    @objc private func collapseToMini() {
        appendLog("collapseToMini (titlebar/menu)")
        // Also drop the file so either path works if UI is mid-reload.
        let url = cmdMiniURL
        try? FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try? "mini\n".write(to: url, atomically: true, encoding: .utf8)
        showMini(activate: true)
    }

    // MARK: - Full window

    private func showFull(activate: Bool) {
        if fullWindow == nil {
            let fallback = NSRect(x: 120, y: 80, width: 1100, height: 780)
            let frame = restoredFrame(key: kFullFrame, fallback: fallback)
            let win = NSWindow(
                contentRect: NSRect(origin: .zero, size: frame.size),
                styleMask: [.titled, .closable, .miniaturizable, .resizable],
                backing: .buffered,
                defer: false
            )
            win.setFrame(frame, display: false)
            win.title = "RAM Cleanup"
            win.isMovable = true
            win.minSize = NSSize(width: 720, height: 480)
            win.isReleasedWhenClosed = false
            win.tabbingMode = .disallowed
            win.delegate = self
            win.level = .normal

            let accessory = NSTitlebarAccessoryViewController()
            let wrap = NSView(frame: NSRect(x: 0, y: 0, width: 96, height: 28))
            let miniBtn = NSButton(title: "Mini", target: self, action: #selector(collapseToMini))
            miniBtn.bezelStyle = .texturedRounded
            miniBtn.font = NSFont.systemFont(ofSize: 11, weight: .medium)
            miniBtn.frame = NSRect(x: 4, y: 2, width: 88, height: 24)
            miniBtn.toolTip = "Shrink to always-on-top widget"
            wrap.addSubview(miniBtn)
            accessory.view = wrap
            accessory.layoutAttribute = .trailing
            win.addTitlebarAccessoryViewController(accessory)

            let config = WKWebViewConfiguration()
            config.userContentController.add(self, name: "ramcleanup")
            let wv = WKWebView(frame: .zero, configuration: config)
            wv.navigationDelegate = self
            wv.autoresizingMask = [.width, .height]
            win.contentView = wv
            webView = wv
            fullWindow = win

            if defaults.string(forKey: kFullFrame) == nil {
                win.center()
            }
            loadUIWhenReady()
            appendLog("full window created")
        }

        guard let full = fullWindow else { return }
        full.level = .normal
        full.orderFrontRegardless()
        full.makeKeyAndOrderFront(nil)
        showingMini = false
        miniWindow?.orderOut(nil)

        if activate {
            NSApp.activate(ignoringOtherApps: true)
        }
    }

    // MARK: - Poll

    private func startPolling() {
        pollTimer?.invalidate()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            self?.refreshMiniStats()
        }
        if let pollTimer {
            RunLoop.main.add(pollTimer, forMode: .common)
        }
    }

    private func refreshMiniStats() {
        guard showingMini, let usedLabel, let detailLabel else { return }
        var req = URLRequest(url: URL(string: "http://\(host):\(port)/api/status")!)
        req.timeoutInterval = 1.2
        URLSession.shared.dataTask(with: req) { [weak self] data, _, _ in
            guard
                let self,
                let data,
                let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                let mem = json["memory"] as? [String: Any]
            else { return }
            let used = mem["used_label"] as? String ?? "—"
            let pressure = (mem["pressure"] as? String ?? "normal").uppercased()
            let pct = mem["used_pct"] as? Double ?? 0
            DispatchQueue.main.async {
                usedLabel.stringValue = used
                detailLabel.stringValue = "\(pressure) · \(String(format: "%.0f", pct))%"
                switch pressure.lowercased() {
                case "critical":
                    usedLabel.textColor = NSColor(calibratedRed: 1, green: 0.45, blue: 0.4, alpha: 1)
                case "warn":
                    usedLabel.textColor = NSColor(calibratedRed: 1, green: 0.85, blue: 0.4, alpha: 1)
                default:
                    usedLabel.textColor = .white
                }
                self.miniWindow?.level = .floating
            }
        }.resume()
    }

    // MARK: - Delegate

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if sender === fullWindow {
            // Closing the full window quits (does not fall into a broken mini state).
            NSApp.terminate(nil)
            return false
        }
        if sender === miniWindow {
            // Closing mini restores full so we never have zero windows mid-session.
            DispatchQueue.main.async { [weak self] in
                self?.showFull(activate: true)
            }
            return false
        }
        return true
    }

    func windowDidMove(_ notification: Notification) { saveFrames() }
    func windowDidResize(_ notification: Notification) { saveFrames() }

    // MARK: - Server

    private var projectRoot: URL {
        let bundle = Bundle.main.bundleURL
        if bundle.pathExtension == "app" {
            return bundle.deletingLastPathComponent()
        }
        if bundle.lastPathComponent == "Contents" {
            return bundle.deletingLastPathComponent().deletingLastPathComponent()
        }
        if bundle.lastPathComponent == "MacOS" {
            return bundle.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        }
        let exe = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
        return exe.deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
    }

    private var pythonPath: String {
        for path in ["/usr/local/bin/python3", "/opt/homebrew/bin/python3", "/usr/bin/python3"] {
            if FileManager.default.isExecutableFile(atPath: path) { return path }
        }
        return "/usr/bin/python3"
    }

    private var logFileURL: URL {
        let logDir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".ramcleanup")
        try? FileManager.default.createDirectory(at: logDir, withIntermediateDirectories: true)
        return logDir.appendingPathComponent("webui.log")
    }

    private func appendLog(_ line: String) {
        let url = logFileURL
        let stamp = ISO8601DateFormatter().string(from: Date())
        let text = "[\(stamp)] \(line)\n"
        if !FileManager.default.fileExists(atPath: url.path) {
            FileManager.default.createFile(atPath: url.path, contents: text.data(using: .utf8))
            return
        }
        guard let handle = try? FileHandle(forWritingTo: url) else { return }
        defer { try? handle.close() }
        handle.seekToEndOfFile()
        if let data = text.data(using: .utf8) { handle.write(data) }
    }

    private func serverAlreadyUp() -> Bool {
        var req = URLRequest(url: URL(string: "http://\(host):\(port)/")!)
        req.timeoutInterval = 0.35
        let sem = DispatchSemaphore(value: 0)
        var ok = false
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            if let http = resp as? HTTPURLResponse, (200..<400).contains(http.statusCode) { ok = true }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 0.45)
        return ok
    }

    private func startServerIfNeeded() {
        if serverAlreadyUp() {
            appendLog("server already up")
            return
        }
        let root = projectRoot
        let script = root.appendingPathComponent("ramcleanup.py")
        let py = pythonPath
        appendLog("projectRoot=\(root.path)")
        appendLog("python=\(py)")
        appendLog("script=\(script.path) exists=\(FileManager.default.fileExists(atPath: script.path))")
        guard FileManager.default.fileExists(atPath: script.path) else {
            DispatchQueue.main.async {
                self.showAlert("Missing ramcleanup.py next to RamCleanup.app\nLooked in:\n\(root.path)")
            }
            return
        }
        let logURL = logFileURL
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        let out = FileHandle(forWritingAtPath: logURL.path)
        out?.seekToEndOfFile()

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: py)
        proc.arguments = [script.path, "--web", "--host", host, "--port", String(port)]
        proc.currentDirectoryURL = root
        proc.standardOutput = out
        proc.standardError = out
        var env = ProcessInfo.processInfo.environment
        let path = env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
        if !path.contains("/usr/local/bin") {
            env["PATH"] = "/usr/local/bin:/opt/homebrew/bin:" + path
        }
        proc.environment = env
        proc.terminationHandler = { [weak self] p in
            self?.appendLog("server exited status=\(p.terminationStatus)")
        }
        do {
            try proc.run()
            server = proc
            appendLog("server pid=\(proc.processIdentifier)")
        } catch {
            appendLog("server start error: \(error)")
            DispatchQueue.main.async {
                self.showAlert("Could not start server: \(error.localizedDescription)")
            }
        }
    }

    private func stopServer() {
        guard let server, server.isRunning else { return }
        server.terminate()
        DispatchQueue.global().async {
            Thread.sleep(forTimeInterval: 0.35)
            if server.isRunning { server.interrupt() }
        }
    }

    private func loadUIWhenReady(attempt: Int = 0) {
        guard let webView else { return }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            if self.serverAlreadyUp() {
                DispatchQueue.main.async {
                    webView.load(URLRequest(url: URL(string: "http://\(self.host):\(self.port)/")!))
                }
                return
            }
            if attempt > 50 {
                DispatchQueue.main.async {
                    self.showAlert("Server did not start. Check ~/.ramcleanup/webui.log")
                }
                return
            }
            DispatchQueue.global().asyncAfter(deadline: .now() + 0.15) {
                self.loadUIWhenReady(attempt: attempt + 1)
            }
        }
    }

    private func showAlert(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "RAM Cleanup"
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.runModal()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
