// kiln-notifier — puts Kiln's print code on the Mac's screen as Kiln.
//
// One job: post the banner a person reads a print code from, under Kiln's
// own name and icon, and say honestly when macOS will not let it.
//
//   kiln-notifier status    prints: authorized | denied | not_determined
//   kiln-notifier request   asks macOS for permission (the system's own
//                           "Kiln would like to send you notifications"),
//                           waits for the answer, prints the new status
//   kiln-notifier post      reads {"title","subtitle","body"} as JSON on
//                           standard input and posts it
//
// The words arrive on standard input, never as arguments: any program on
// the machine can list every process's arguments, and the code is a secret.
//
// Exit codes: 0 done, 2 bad input, 3 not allowed to post, 4 post failed.

import Foundation
import UserNotifications

let center = UNUserNotificationCenter.current()

/// Run *work*, whose completion calls `finish`, and wait for it without
/// blocking the thread the system answers on.
func wait(seconds: Double, _ work: (@escaping () -> Void) -> Void) {
    var finished = false
    work { finished = true }
    let deadline = Date().addingTimeInterval(seconds)
    while !finished && Date() < deadline {
        RunLoop.current.run(until: Date().addingTimeInterval(0.05))
    }
}

func currentStatus() -> String {
    var answer = "not_determined"
    wait(seconds: 5) { finish in
        center.getNotificationSettings { settings in
            switch settings.authorizationStatus {
            case .authorized, .provisional, .ephemeral:
                answer = settings.alertSetting == .disabled ? "denied" : "authorized"
            case .denied:
                answer = "denied"
            default:
                answer = "not_determined"
            }
            finish()
        }
    }
    return answer
}

struct Words: Decodable {
    let title: String
    let subtitle: String
    let body: String
}

switch CommandLine.arguments.dropFirst().first ?? "" {
case "status":
    print(currentStatus())
    exit(0)

case "request":
    // The person is asked at most once; macOS remembers the answer, and a
    // second request returns it without asking again.
    wait(seconds: 120) { finish in
        center.requestAuthorization(options: [.alert, .sound]) { _, _ in finish() }
    }
    print(currentStatus())
    exit(0)

case "post":
    let input = FileHandle.standardInput.readDataToEndOfFile()
    guard let words = try? JSONDecoder().decode(Words.self, from: input) else { exit(2) }
    guard currentStatus() == "authorized" else { exit(3) }
    let content = UNMutableNotificationContent()
    content.title = words.title
    content.subtitle = words.subtitle
    content.body = words.body
    content.sound = .default
    var failed = true
    wait(seconds: 10) { finish in
        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        center.add(request) { error in
            failed = error != nil
            finish()
        }
    }
    exit(failed ? 4 : 0)

default:
    FileHandle.standardError.write("usage: kiln-notifier status | request | post\n".data(using: .utf8)!)
    exit(2)
}
