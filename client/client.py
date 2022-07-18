import json
import socket
import subprocess
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import List

import jc
import pynetbox
import requests

pool = ThreadPoolExecutor()


def get_drives():
    results = []

    command = "lsblk --json -d -o NAME,HCTL,type"
    command_output = subprocess.check_output(command, shell=True)

    for device in json.loads(command_output)["blockdevices"]:
        device_name = device["name"]

        # Skip ZFS devices.
        if device_name.startswith("zd"):
            continue

        # Filter for disks.
        if device["type"] == "disk":
            results.append(device)

    return results


def get_smart(drive: dict):
    drive_path = f"/dev/{drive['name']}"
    command = ["smartctl", "-xj", drive_path]

    try:
        command_output = subprocess.check_output(command)
    except subprocess.CalledProcessError:
        return drive_path, None

    return (drive["hctl"], json.loads(command_output))


def parse_smart(smart_info):
    result = {
        "name": smart_info["device"]["name"],
        # "vendor": smart_info["vendor"],
        # "product": smart_info["product"],
        # "revision": smart_info["revision"],
        "model": smart_info.get(
            "model_name", smart_info.get("scsi_model_name", "Unknown")
        ),
        "serial": smart_info["serial_number"],
        "size": smart_info["user_capacity"]["bytes"],
        "power_on_time": smart_info["power_on_time"],
    }

    return result


def get_smart_for_all_drives():
    results = {}

    drives = get_drives()
    for device_id, smart_result in pool.map(get_smart, drives):
        if smart_result:
            results[device_id] = parse_smart(smart_result)

    return results


def get_dmidecode():
    command_output = subprocess.check_output("dmidecode").decode("utf8")
    return jc.parse("dmidecode", command_output)


def merge_keys(source: dict, destination: dict, keys: List[str]):
    for key, value in source.items():
        if key in keys:
            destination[key] = value


SYSTEM_INFO_KEYS = ["uuid", "product_name", "manufacturer"]


def parse_dmi_info(dmi_info):
    result = {}

    memory = {}
    serial = None
    alt_serial = None

    for section in dmi_info:
        values = section["values"]

        if section["type"] == 1:
            serial = values["serial_number"]
            merge_keys(values, result, SYSTEM_INFO_KEYS)
        elif section["type"] == 2:
            alt_serial = values["serial_number"]
        elif section["type"] == 17:
            print(values)
            speed = values.get("speed", "Unknown")
            memory[values["locator"]] = {
                "size": values["size"],
                "speed": speed,
                "model": values.get("part_number", "Unknown"),
                "serial": values.get("serial_number", "Unknown"),
                "description": f"{values['size']} - {speed}",
            }

    if memory:
        result["memory"] = memory

    # use alt_serial if serial is a placeholder.
    if serial in ["0123456789"]:
        serial = alt_serial
    result["serial"] = serial

    return result


def update_fields(
    data,
    device,
    device_type,
    inventory_dict,
    stuff_to_add,
    stuff_to_update,
    stuff_to_remove,
):
    for drive_id, drive in data.items():
        serial = drive["serial"]
        tags = [
            {"name": "autogen"},
            {"name": device_type},
        ]

        payload = {
            "name": drive_id,
            "part_id": drive["model"],
            "serial": drive["serial"],
            "tags": tags,
        }

        if "description" in drive:
            payload["description"] = drive["description"]

        if drive_id in inventory_dict:
            try:
                stuff_to_remove.pop(drive_id)
            except ValueError:
                pass

            if inventory_dict[drive_id].serial != serial:
                payload.update(
                    {
                        "id": inventory_dict[drive_id].id,
                    }
                )
                stuff_to_update.append(payload)
            continue

        payload.update({"device": device.id})
        stuff_to_add.append(payload)


def update_netbox(device_info):
    nb = pynetbox.api(
        "https://netbox.generalprogramming.org/",
        # XXX/TODO: Stop hardcoding this.
        token="amogus",
    )

    device = nb.dcim.devices.get(serial=device_info["serial"])

    # combined add/update state
    stuff_to_add = defaultdict(lambda: [])
    stuff_to_update = defaultdict(lambda: [])

    # get interfaces + inventory items
    interfaces = nb.dcim.interfaces.filter(device=device.name)
    interfaces_dict = {i.name: i for i in interfaces}
    interfaces_by_mac = {i.mac_address: i for i in interfaces_dict.values()}

    inventory_items = nb.dcim.inventory_items.filter(device_id=device.id, tag="autogen")
    inventory_dict = {i.name: i for i in inventory_items}

    # update interfaces
    for interface in device_info["interfaces"]:
        interface_name = interface["ifname"]
        mac_address = interface["address"].upper()

        payload = {
            "device": device.id,
            "name": interface_name,
            "mac_address": mac_address,
        }

        if mac_address in interfaces_by_mac or interface_name in interfaces_dict:
            existing_interface = interfaces_by_mac.get(
                mac_address
            ) or interfaces_dict.get(interface_name)
            print(existing_interface)

            if (
                existing_interface.mac_address != mac_address
                or existing_interface.name != interface_name
            ):
                existing_interface.mac_address = mac_address
                existing_interface.name = interface_name
                stuff_to_update["interfaces"].append(existing_interface)
        else:
            payload.update(
                {
                    # XXX: hacky way to get netbox to create a new interface
                    "type": "1000base-t",
                }
            )
            stuff_to_add["interfaces"].append(payload)

    # add all drives + ram to inventory
    stuff_to_remove = {"inventory": inventory_dict.copy()}

    update_fields(
        device_info["drives"],
        device,
        "drive",
        inventory_dict,
        stuff_to_add["inventory"],
        stuff_to_update["inventory"],
        stuff_to_remove["inventory"],
    )

    update_fields(
        device_info["memory"],
        device,
        "memory",
        inventory_dict,
        stuff_to_add["inventory"],
        stuff_to_update["inventory"],
        stuff_to_remove["inventory"],
    )

    print("ADD", stuff_to_add)
    print("UPDATE", stuff_to_update)
    print("REMOVE", stuff_to_remove)

    # update interfaces
    nb.dcim.interfaces.create(stuff_to_add["interfaces"])
    nb.dcim.interfaces.update(stuff_to_update["interfaces"])

    # update inventory
    nb.dcim.inventory_items.create(stuff_to_add.get("inventory", []))
    nb.dcim.inventory_items.update(stuff_to_update.get("inventory", []))
    nb.dcim.inventory_items.delete(list(stuff_to_remove["inventory"].values()))


def get_interfaces():
    # get normal interfaces
    command = "ip -j link"
    interfaces = json.loads(subprocess.check_output(command, shell=True))

    output = []

    for interface in interfaces:
        int_name = interface["ifname"]

        if not (int_name.startswith("en") or int_name.startswith("eth")):
            continue

        output.append(interface)

    # get ipmi info
    try:
        command = "ipmitool lan print"
        ipmi_info = subprocess.check_output(command, shell=True, timeout=5).decode(
            "utf8"
        )
        ipmi_parsed = jc.parse("kv", ipmi_info)

        output.append(
            {
                "ifname": "ipmi",
                "address": ipmi_parsed["mac address"].upper(),
            }
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass

    return output


if __name__ == "__main__":
    dmi_info = get_dmidecode()

    payload = {
        "drives": get_smart_for_all_drives(),
        "interfaces": get_interfaces(),
        # "dmi": dmi_info,
    }

    payload.update(parse_dmi_info(dmi_info))
    print(json.dumps(payload, indent=4))

    update_netbox(payload)

    # XXX / TODO: add signature
    req = requests.post(
        "https://bootstraper.butt.workers.dev/assimilate/json", json=payload
    )

    print(req.json())
