package dev.fedoralink.android

import android.Manifest
import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.widget.AdapterView
import android.widget.ArrayAdapter
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import dev.fedoralink.android.databinding.ActivityMainBinding
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.launch

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private var devices: List<BluetoothDevice> = emptyList()

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions(),
    ) { refresh() }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.enableSwitch.setOnCheckedChangeListener { button, checked ->
            // Ignore programmatic updates from refresh().
            if (!button.isPressed) return@setOnCheckedChangeListener
            if (checked) {
                if (!hasBluetoothPermissions()) {
                    button.isChecked = false
                    requestBluetoothPermissions()
                } else {
                    LinkService.start(this)
                }
            } else {
                LinkService.stop(this)
            }
        }

        binding.permissionsButton.setOnClickListener { requestBluetoothPermissions() }

        binding.notificationAccessButton.setOnClickListener {
            startActivity(
                Intent(android.provider.Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)
            )
        }

        binding.sendClipboardButton.setOnClickListener {
            val message = when {
                !LinkManager.isConnected() -> R.string.clipboard_not_connected
                ClipboardBridge.sendToPc(this) -> R.string.clipboard_sent
                else -> R.string.clipboard_empty
            }
            Toast.makeText(this, message, Toast.LENGTH_SHORT).show()
        }

        binding.deviceSpinner.onItemSelectedListener =
            object : AdapterView.OnItemSelectedListener {
                override fun onItemSelected(
                    parent: AdapterView<*>?, view: android.view.View?,
                    position: Int, id: Long,
                ) {
                    devices.getOrNull(position)?.let {
                        Settings.setPairedAddress(this@MainActivity, it.address)
                    }
                }

                override fun onNothingSelected(parent: AdapterView<*>?) = Unit
            }

        lifecycleScope.launch {
            combine(LinkManager.state, LinkManager.peerName) { state, peer ->
                when (state) {
                    LinkManager.State.CONNECTED ->
                        getString(R.string.status_connected_to, peer ?: "PC")
                    LinkManager.State.WAITING -> getString(R.string.status_waiting)
                    LinkManager.State.STOPPED -> getString(R.string.status_disconnected)
                }
            }.collect { binding.statusText.text = it }
        }
    }

    override fun onResume() {
        super.onResume()
        refresh()
    }

    private fun refresh() {
        binding.enableSwitch.isChecked = Settings.isEnabled(this)

        val granted = hasBluetoothPermissions()
        binding.permissionsButton.isEnabled = !granted
        binding.permissionsButton.setText(
            if (granted) R.string.permissions_granted else R.string.grant_permissions
        )

        val listening = NotificationRelay.isEnabled(this)
        binding.notificationAccessButton.setText(
            if (listening) R.string.notification_access_granted
            else R.string.grant_notification_access
        )

        loadPairedDevices()
    }

    @SuppressLint("MissingPermission")
    private fun loadPairedDevices() {
        if (!hasBluetoothPermissions()) {
            devices = emptyList()
            binding.deviceSpinner.adapter = simpleAdapter(
                listOf(getString(R.string.grant_permissions))
            )
            binding.deviceSpinner.isEnabled = false
            return
        }

        val adapter = getSystemService(BluetoothManager::class.java)?.adapter
        devices = try {
            adapter?.bondedDevices?.toList().orEmpty()
        } catch (_: SecurityException) {
            emptyList()
        }

        if (devices.isEmpty()) {
            binding.deviceSpinner.adapter = simpleAdapter(
                listOf(getString(R.string.no_paired_devices))
            )
            binding.deviceSpinner.isEnabled = false
            return
        }

        binding.deviceSpinner.isEnabled = true
        binding.deviceSpinner.adapter = simpleAdapter(
            devices.map { it.name ?: it.address }
        )

        val saved = Settings.pairedAddress(this)
        val index = devices.indexOfFirst { it.address == saved }
        if (index >= 0) binding.deviceSpinner.setSelection(index)
    }

    private fun simpleAdapter(items: List<String>) =
        ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, items)

    private fun requiredPermissions(): Array<String> = buildList {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            add(Manifest.permission.BLUETOOTH_CONNECT)
            add(Manifest.permission.BLUETOOTH_SCAN)
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            add(Manifest.permission.POST_NOTIFICATIONS)
        }
    }.toTypedArray()

    private fun hasBluetoothPermissions(): Boolean =
        requiredPermissions().all {
            ContextCompat.checkSelfPermission(this, it) == PackageManager.PERMISSION_GRANTED
        }

    private fun requestBluetoothPermissions() {
        val missing = requiredPermissions()
        if (missing.isEmpty()) {
            refresh()
            return
        }
        permissionLauncher.launch(missing)
    }
}
