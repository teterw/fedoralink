plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Supplied by CI from repository secrets. Absent on a normal local build,
// in which case the release APK comes out unsigned rather than failing.
val keystorePath: String? = System.getenv("SIGNING_KEYSTORE_PATH")

android {
    namespace = "dev.fedoralink.android"
    compileSdk = 35

    defaultConfig {
        applicationId = "dev.fedoralink.android"
        // Android 8.0. Below this there's no usable foreground-service
        // story for holding a Bluetooth socket open.
        minSdk = 26
        targetSdk = 35
        // Overridable so a tagged CI build stamps the tag rather than
        // needing a commit every time the version changes.
        versionCode = (System.getenv("VERSION_CODE") ?: "2").toInt()
        versionName = System.getenv("VERSION_NAME") ?: "0.2.0"
    }

    signingConfigs {
        create("release") {
            if (keystorePath != null) {
                storeFile = file(keystorePath)
                storePassword = System.getenv("SIGNING_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("SIGNING_KEY_ALIAS")
                keyPassword = System.getenv("SIGNING_KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            // A debug-signed APK is one of the loudest signals Play Protect
            // looks for, which is the whole reason this config exists.
            signingConfig =
                if (keystorePath != null) signingConfigs.getByName("release") else null
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        viewBinding = true
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
}
