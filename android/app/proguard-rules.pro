# Release builds don't currently minify (see build.gradle.kts), so this
# file exists mainly so the release build type has somewhere to grow.

# NotificationListenerService and TileService are instantiated by the
# platform by name — never let R8 rename or strip them.
-keep class dev.fedoralink.android.NotificationRelay { *; }
-keep class dev.fedoralink.android.ClipboardTileService { *; }
-keep class dev.fedoralink.android.LinkService { *; }
-keep class dev.fedoralink.android.BootReceiver { *; }
