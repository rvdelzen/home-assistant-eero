class EeroIpTableCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._filters = {
      status: "",
      online: "",
      node: "",
      manufacturer: "",
      critical: false,
      query: "",
    };
    this._sort = { key: "name", dir: 1 };
    this._rows = [];
    this._summary = {};
    this._entryId = "";
    this._error = "";
    this._busy = false;
    this._timer = null;
    this._loaded = false;
  }

  setConfig(config) {
    this._config = config || {};
    const filters = config.filters || config.default_filters || {};
    this._filters = {
      status: filters.status || "",
      online: filters.online || "",
      node: filters.node || "",
      manufacturer: filters.manufacturer || "",
      critical: Boolean(filters.critical || filters.critical_only),
      query: filters.query || "",
    };
    if (config.config_entry_id) {
      this._entryId = config.config_entry_id;
    }
    if (this._hass) {
      this._render();
    }
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._loaded) {
      this._loaded = true;
      this._render();
      this._load(false);
      this._timer = setInterval(() => this._load(false), 60000);
    }
  }

  disconnectedCallback() {
    if (this._timer) {
      clearInterval(this._timer);
      this._timer = null;
    }
  }

  getCardSize() {
    return 6;
  }

  async _service(name, data) {
    const payload = { ...data };
    if (this._entryId) {
      payload.config_entry_id = this._entryId;
    }
    const result = await this._hass.callService("eero", name, payload, {}, true);
    if (
      result &&
      typeof result === "object" &&
      result.response &&
      !result.devices &&
      !result.changes &&
      !result.before
    ) {
      return result.response;
    }
    return result || {};
  }

  async _load(refresh) {
    if (!this._hass) {
      return;
    }
    this._busy = true;
    this._render();
    try {
      const table = await this._service("ip_table", { refresh: Boolean(refresh) });
      this._rows = table.devices || [];
      this._summary = table.summary || {};
      if (table.config_entry_id) {
        this._entryId = table.config_entry_id;
      }
      this._error = "";
    } catch (err) {
      this._error = (err && err.message) || String(err);
    } finally {
      this._busy = false;
      this._render();
    }
  }

  async _reserve(row, suggest) {
    this._error = "";
    try {
      const data = { mac: row.mac };
      if (suggest) {
        data.suggest = true;
      } else if (row.ip) {
        data.ip = row.ip;
      }
      await this._service("reserve_ip", data);
      await this._load(true);
    } catch (err) {
      this._error = (err && err.message) || String(err);
      this._render();
    }
  }

  async _release(row) {
    const label = row.name || row.mac;
    if (!window.confirm(`Release the reservation for ${label} (${row.mac})?`)) {
      return;
    }
    this._error = "";
    try {
      await this._service("release_ip", { mac: row.mac });
      await this._load(true);
    } catch (err) {
      this._error = (err && err.message) || String(err);
      this._render();
    }
  }

  async _syncNames() {
    if (!this._entryId) {
      this._error =
        "Set config_entry_id on the card when more than one Eero entry is loaded.";
      this._render();
      return;
    }
    this._error = "";
    this._busy = true;
    this._render();
    try {
      const preview = await this._service("sync_names", {
        dry_run: true,
        targets: ["nickname", "reservation"],
        prefix_area: "when_missing",
      });
      const changes = preview.changes || [];
      const updates = changes.filter((row) => row.action === "update");
      const flagged = changes.filter((row) => row.action === "flagged");
      const unchanged = changes.filter((row) => row.action === "unchanged").length;
      const lines = []
        .concat(
          updates.map((row) => {
            const fields = (row.fields || []).join(", ") || "name";
            return `Update ${row.mac} (${fields}): ${row.current_nickname || "—"} / ${row.current_description || "—"} → ${row.proposed_name}`;
          }),
          flagged.map(
            (row) =>
              `Skip ${row.mac} (${row.reason}): ${row.proposed_name || "no name"}`
          )
        );
      const summary = `${updates.length} to update, ${flagged.length} flagged, ${unchanged} unchanged.`;
      if (!updates.length) {
        window.alert(`Nothing to update.\n\n${summary}\n${lines.join("\n")}`);
        return;
      }
      const shown = lines.slice(0, 40).join("\n");
      const more = lines.length > 40 ? `\n… ${lines.length - 40} more` : "";
      if (!window.confirm(`Sync names from Home Assistant to eero?\n\n${summary}\n${shown}${more}`)) {
        return;
      }
      await this._service("sync_names", {
        dry_run: false,
        targets: ["nickname", "reservation"],
        prefix_area: "when_missing",
      });
      await this._load(true);
    } catch (err) {
      this._error = (err && err.message) || String(err);
      this._render();
    } finally {
      this._busy = false;
      this._render();
    }
  }

  _visibleRows() {
    const query = (this._filters.query || "").trim().toLowerCase();
    const rows = this._rows.filter((row) => {
      if (this._filters.status && row.status !== this._filters.status) {
        return false;
      }
      if (this._filters.online === "online" && !row.connected) {
        return false;
      }
      if (this._filters.online === "offline" && row.connected) {
        return false;
      }
      if (this._filters.node && (row.node || "") !== this._filters.node) {
        return false;
      }
      if (
        this._filters.manufacturer &&
        (row.manufacturer || "") !== this._filters.manufacturer
      ) {
        return false;
      }
      if (this._filters.critical && !row.critical) {
        return false;
      }
      if (!query) {
        return true;
      }
      const haystack = [row.name, row.ip, row.reserved_ip, row.mac, row.hostname, row.manufacturer]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return haystack.includes(query);
    });
    const { key, dir } = this._sort;
    rows.sort((left, right) => {
      const a = left[key] == null ? "" : String(left[key]).toLowerCase();
      const b = right[key] == null ? "" : String(right[key]).toLowerCase();
      if (a < b) {
        return -1 * dir;
      }
      if (a > b) {
        return 1 * dir;
      }
      return 0;
    });
    return rows;
  }

  _options(key) {
    const values = new Set();
    this._rows.forEach((row) => {
      if (row[key]) {
        values.add(row[key]);
      }
    });
    return Array.from(values).sort((a, b) => String(a).localeCompare(String(b)));
  }

  _render() {
    const root = this.shadowRoot;
    root.replaceChildren();
    const style = document.createElement("style");
    style.textContent = `
      :host { display: block; }
      .card { padding: 16px; }
      .toolbar, .filters { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 12px; }
      button, select, input { font: inherit; }
      button { cursor: pointer; }
      table { width: 100%; border-collapse: collapse; }
      th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--divider-color, #e0e0e0); vertical-align: top; }
      th { cursor: pointer; }
      .badge { display: inline-block; padding: 1px 6px; border-radius: 8px; background: var(--secondary-background-color, #eee); }
      .flag { color: var(--error-color, #b00020); font-weight: 600; }
      .error { color: var(--error-color, #b00020); margin-bottom: 8px; white-space: pre-wrap; }
      .summary { opacity: 0.8; margin-bottom: 8px; }
      .actions { display: flex; gap: 4px; }
    `;
    root.append(style);
    const card = document.createElement("ha-card");
    const body = document.createElement("div");
    body.className = "card";
    const title = document.createElement("h3");
    title.textContent = "Eero IP reservations";
    body.append(title);
    if (this._error) {
      const error = document.createElement("div");
      error.className = "error";
      error.textContent = this._error;
      body.append(error);
    }
    const summary = this._summary || {};
    const summaryLine = document.createElement("div");
    summaryLine.className = "summary";
    summaryLine.textContent = this._busy
      ? "Loading…"
      : `${summary.reservations || 0} reserved · ${summary.dynamic || 0} dynamic · ${summary.fixed_mismatch || 0} mismatch · ${summary.stale_reservation || 0} stale · ${summary.critical_unreserved || 0} critical without a reservation`;
    body.append(summaryLine);

    const toolbar = document.createElement("div");
    toolbar.className = "toolbar";
    const refresh = document.createElement("button");
    refresh.type = "button";
    refresh.textContent = "Refresh";
    refresh.addEventListener("click", () => this._load(true));
    const sync = document.createElement("button");
    sync.type = "button";
    sync.textContent = "Sync names";
    sync.addEventListener("click", () => this._syncNames());
    toolbar.append(refresh, sync);
    body.append(toolbar);

    const filters = document.createElement("div");
    filters.className = "filters";
    filters.append(
      this._select("status", "Status", ["", "fixed", "dynamic", "fixed_mismatch", "stale_reservation"], {
        "": "Any status",
        fixed: "Fixed",
        dynamic: "Dynamic",
        fixed_mismatch: "Mismatch",
        stale_reservation: "Stale",
      }),
      this._select("online", "Online", ["", "online", "offline"], {
        "": "Any presence",
        online: "Online",
        offline: "Offline",
      }),
      this._select(
        "node",
        "Node",
        [""].concat(this._options("node")),
        { "": "Any node" }
      ),
      this._select(
        "manufacturer",
        "Manufacturer",
        [""].concat(this._options("manufacturer")),
        { "": "Any manufacturer" }
      )
    );
    const critical = document.createElement("label");
    const criticalBox = document.createElement("input");
    criticalBox.type = "checkbox";
    criticalBox.checked = this._filters.critical;
    criticalBox.addEventListener("change", () => {
      this._filters.critical = criticalBox.checked;
      this._render();
    });
    critical.append(criticalBox, document.createTextNode(" Critical only"));
    const search = document.createElement("input");
    search.type = "search";
    search.placeholder = "Search";
    search.value = this._filters.query;
    search.addEventListener("input", () => {
      this._filters.query = search.value;
      const tbody = this.shadowRoot.querySelector("tbody");
      if (!tbody) {
        this._render();
        return;
      }
      tbody.replaceChildren();
      this._visibleRows().forEach((row) => tbody.append(this._row(row)));
    });
    filters.append(critical, search);
    body.append(filters);

    const table = document.createElement("table");
    const head = document.createElement("tr");
    [
      ["name", "Name"],
      ["ip", "IP"],
      ["status", "Fixed"],
      ["mac", "MAC"],
      ["manufacturer", "Manufacturer"],
      ["node", "Node"],
      ["connected", "Online"],
      ["actions", ""],
    ].forEach(([key, label]) => {
      const cell = document.createElement("th");
      cell.textContent = label;
      if (key !== "actions") {
        cell.addEventListener("click", () => {
          if (this._sort.key === key) {
            this._sort.dir *= -1;
          } else {
            this._sort = { key, dir: 1 };
          }
          this._render();
        });
      }
      head.append(cell);
    });
    const thead = document.createElement("thead");
    thead.append(head);
    table.append(thead);
    const tbody = document.createElement("tbody");
    this._visibleRows().forEach((row) => tbody.append(this._row(row)));
    table.append(tbody);
    body.append(table);
    card.append(body);
    root.append(card);
  }

  _select(key, aria, values, labels) {
    const select = document.createElement("select");
    select.setAttribute("aria-label", aria);
    values.forEach((value) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = labels[value] || value;
      option.selected = this._filters[key] === value;
      select.append(option);
    });
    select.addEventListener("change", () => {
      this._filters[key] = select.value;
      this._render();
    });
    return select;
  }

  _row(row) {
    const tr = document.createElement("tr");
    const name = document.createElement("td");
    if (row.critical && !row.reserved_ip) {
      const flag = document.createElement("span");
      flag.className = "flag";
      flag.textContent = "● ";
      name.append(flag);
    }
    name.append(document.createTextNode(row.name || row.mac));
    const ip = document.createElement("td");
    ip.textContent = row.ip || row.reserved_ip || "";
    const status = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = row.status || "";
    status.append(badge);
    const mac = document.createElement("td");
    mac.textContent = row.mac || "";
    const manufacturer = document.createElement("td");
    manufacturer.textContent = row.manufacturer || "";
    const node = document.createElement("td");
    node.textContent = row.node || "";
    const online = document.createElement("td");
    online.textContent = row.connected ? "online" : row.last_active || "offline";
    const actions = document.createElement("td");
    actions.className = "actions";
    if (!row.reserved_ip && row.ip) {
      const pin = document.createElement("button");
      pin.type = "button";
      pin.textContent = "Pin";
      pin.addEventListener("click", () => this._reserve(row, false));
      actions.append(pin);
    } else if (!row.reserved_ip && !row.ip) {
      const suggest = document.createElement("button");
      suggest.type = "button";
      suggest.textContent = "Suggest";
      suggest.addEventListener("click", () => this._reserve(row, true));
      actions.append(suggest);
    }
    if (row.reserved_ip) {
      const release = document.createElement("button");
      release.type = "button";
      release.textContent = "Release";
      release.addEventListener("click", () => this._release(row));
      actions.append(release);
    }
    tr.append(name, ip, status, mac, manufacturer, node, online, actions);
    return tr;
  }
}

if (!customElements.get("eero-ip-table-card")) {
  customElements.define("eero-ip-table-card", EeroIpTableCard);
}

window.customCards = window.customCards || [];
window.customCards.push({
  type: "eero-ip-table-card",
  name: "Eero IP table",
  description: "DHCP reservations and devices for an Eero network.",
});
