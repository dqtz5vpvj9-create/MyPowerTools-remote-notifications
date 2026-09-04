#import <AppKit/AppKit.h>
#import <Foundation/Foundation.h>
#import <UserNotifications/UserNotifications.h>

#include <dispatch/dispatch.h>
#include <stdint.h>

enum RemoteNotificationsMacStatus : int {
    RemoteNotificationsMacOk = 0,
    RemoteNotificationsMacOsUnsupported = -1,
    RemoteNotificationsMacAuthorizationDenied = -30,
    RemoteNotificationsMacRequestFailed = -31,
    RemoteNotificationsMacTimedOut = -32,
    RemoteNotificationsMacNoBundle = 2,
    RemoteNotificationsMacUnavailable = 3,
};

static NSString *StringFromUtf8(const char *value) {
    if (value == nullptr) {
        return @"";
    }

    NSString *result = [NSString stringWithUTF8String:value];
    return result == nil ? @"" : result;
}

static dispatch_time_t DeadlineFromMilliseconds(int timeoutMilliseconds) {
    const int boundedTimeout = timeoutMilliseconds <= 0
        ? 10000
        : MIN(timeoutMilliseconds, 60000);
    return dispatch_time(
        DISPATCH_TIME_NOW,
        static_cast<int64_t>(boundedTimeout) * NSEC_PER_MSEC);
}

@interface RemoteNotificationsMacDelegate : NSObject <UNUserNotificationCenterDelegate>
@end

@implementation RemoteNotificationsMacDelegate

- (void)userNotificationCenter:(UNUserNotificationCenter *)center
       willPresentNotification:(UNNotification *)notification
         withCompletionHandler:(void (^)(UNNotificationPresentationOptions options))completionHandler {
    (void)center;
    (void)notification;
    completionHandler(UNNotificationPresentationOptionBanner |
                      UNNotificationPresentationOptionList |
                      UNNotificationPresentationOptionSound);
}

- (void)userNotificationCenter:(UNUserNotificationCenter *)center
 didReceiveNotificationResponse:(UNNotificationResponse *)response
         withCompletionHandler:(void (^)(void))completionHandler {
    (void)center;
    NSString *activationUri = response.notification.request.content.userInfo[@"activationUri"];
    if ([activationUri isKindOfClass:NSString.class] && activationUri.length > 0) {
        NSURL *url = [NSURL URLWithString:activationUri];
        if (url != nil) {
            [NSWorkspace.sharedWorkspace openURL:url];
        }
    }
    completionHandler();
}

@end

static RemoteNotificationsMacDelegate *NotificationDelegateInstance(void) {
    static RemoteNotificationsMacDelegate *instance;
    static dispatch_once_t onceToken;
    dispatch_once(&onceToken, ^{
        instance = [[RemoteNotificationsMacDelegate alloc] init];
    });
    return instance;
}

static int ResolveAuthorization(
    UNUserNotificationCenter *center,
    dispatch_time_t deadline) API_AVAILABLE(macos(11.0)) {
    dispatch_semaphore_t settingsSemaphore = dispatch_semaphore_create(0);
    __block UNAuthorizationStatus authorizationStatus = UNAuthorizationStatusNotDetermined;
    __block BOOL settingsReceived = NO;
    [center getNotificationSettingsWithCompletionHandler:^(UNNotificationSettings *settings) {
        authorizationStatus = settings.authorizationStatus;
        settingsReceived = YES;
        dispatch_semaphore_signal(settingsSemaphore);
    }];

    if (dispatch_semaphore_wait(settingsSemaphore, deadline) != 0 || !settingsReceived) {
        return RemoteNotificationsMacTimedOut;
    }

    if (authorizationStatus == UNAuthorizationStatusDenied) {
        return RemoteNotificationsMacAuthorizationDenied;
    }

    if (authorizationStatus == UNAuthorizationStatusAuthorized ||
        authorizationStatus == UNAuthorizationStatusProvisional) {
        return RemoteNotificationsMacOk;
    }

    if (authorizationStatus != UNAuthorizationStatusNotDetermined) {
        return RemoteNotificationsMacAuthorizationDenied;
    }

    dispatch_semaphore_t authorizationSemaphore = dispatch_semaphore_create(0);
    __block BOOL granted = NO;
    __block NSError *authorizationError = nil;
    [center requestAuthorizationWithOptions:(UNAuthorizationOptionAlert |
                                              UNAuthorizationOptionSound |
                                              UNAuthorizationOptionBadge)
                          completionHandler:^(BOOL didGrant, NSError *error) {
        granted = didGrant;
        authorizationError = error;
        dispatch_semaphore_signal(authorizationSemaphore);
    }];

    if (dispatch_semaphore_wait(authorizationSemaphore, deadline) != 0) {
        return RemoteNotificationsMacTimedOut;
    }
    if (authorizationError != nil) {
        return RemoteNotificationsMacRequestFailed;
    }
    return granted
        ? RemoteNotificationsMacOk
        : RemoteNotificationsMacAuthorizationDenied;
}

extern "C" int remote_notifications_mac_publish(
    const char *identifier,
    const char *title,
    const char *body,
    const char *activationUri,
    int timeoutMilliseconds) {
    @autoreleasepool {
        if (@available(macOS 11.0, *)) {
            if (NSBundle.mainBundle.bundleIdentifier.length == 0) {
                return RemoteNotificationsMacNoBundle;
            }

            @try {
                UNUserNotificationCenter *center = UNUserNotificationCenter.currentNotificationCenter;
                if (center == nil) {
                    return RemoteNotificationsMacUnavailable;
                }

                center.delegate = NotificationDelegateInstance();
                const dispatch_time_t deadline = DeadlineFromMilliseconds(timeoutMilliseconds);
                const int authorization = ResolveAuthorization(center, deadline);
                if (authorization != RemoteNotificationsMacOk) {
                    return authorization;
                }

                UNMutableNotificationContent *content = [[UNMutableNotificationContent alloc] init];
                content.title = StringFromUtf8(title);
                content.body = StringFromUtf8(body);
                content.sound = UNNotificationSound.defaultSound;
                content.threadIdentifier = @"remote-notifications";
                if (@available(macOS 12.0, *)) {
                    content.interruptionLevel = UNNotificationInterruptionLevelActive;
                }

                NSString *uri = StringFromUtf8(activationUri);
                if (uri.length > 0) {
                    content.userInfo = @{ @"activationUri": uri };
                }

                NSString *requestIdentifier = StringFromUtf8(identifier);
                if (requestIdentifier.length == 0) {
                    requestIdentifier = NSUUID.UUID.UUIDString;
                }
                UNNotificationRequest *request = [UNNotificationRequest
                    requestWithIdentifier:requestIdentifier
                    content:content
                    trigger:nil];

                dispatch_semaphore_t deliverySemaphore = dispatch_semaphore_create(0);
                __block NSError *deliveryError = nil;
                [center addNotificationRequest:request withCompletionHandler:^(NSError *error) {
                    deliveryError = error;
                    dispatch_semaphore_signal(deliverySemaphore);
                }];

                if (dispatch_semaphore_wait(deliverySemaphore, deadline) != 0) {
                    return RemoteNotificationsMacTimedOut;
                }
                return deliveryError == nil
                    ? RemoteNotificationsMacOk
                    : RemoteNotificationsMacRequestFailed;
            } @catch (NSException *exception) {
                (void)exception;
                return RemoteNotificationsMacUnavailable;
            }
        }

        return RemoteNotificationsMacOsUnsupported;
    }
}
