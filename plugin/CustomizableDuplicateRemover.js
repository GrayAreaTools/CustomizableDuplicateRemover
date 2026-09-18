// Customizable Duplicate Remover — Stash UI page.
//
// Stash's built-in Scene Duplicate Checker is not patchable: its component is never
// wrapped by PatchComponent and is explicitly excluded from the loadable registry, so
// its selection dropdown cannot be extended. This registers a separate page with the
// same shape and adds the codec-aware options the built-in one lacks.
//
// All ranking and every guard rail live in the Python backend and are reached through
// runPluginOperation. This file renders a plan and collects a selection; it never
// decides which file to keep, so the policy cannot drift between the two languages.
//
// Loaded as a classic script and concatenated with the plugin's other JS, so: no ES
// modules, no JSX, and React comes from PluginApi.

(function () {
  "use strict";

  const PluginApi = window.PluginApi;
  if (!PluginApi) return;

  const React = PluginApi.React;
  const { Button, Form, Dropdown, Alert, Spinner } = PluginApi.libraries.Bootstrap;
  const { NavLink, Link } = PluginApi.libraries.ReactRouterDOM;

  const PLUGIN_ID = "CustomizableDuplicateRemover";
  const ROUTE = "/plugin/customizable-duplicate-remover";

  // Honour a reverse-proxy base href, which unRAID setups often have.
  const baseURL =
    (document.querySelector("base") && document.querySelector("base").getAttribute("href")) || "/";

  function callGQL(query, variables) {
    return fetch(baseURL + "graphql", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ query: query, variables: variables || {} }),
    })
      .then(function (res) {
        if (!res.ok) throw new Error("Stash returned HTTP " + res.status);
        return res.json();
      })
      .then(function (body) {
        if (body.errors && body.errors.length) {
          throw new Error(body.errors.map(function (e) { return e.message; }).join("; "));
        }
        return body.data;
      });
  }

  // runPluginOperation runs the plugin synchronously and returns its output.
  function runOperation(args) {
    return callGQL(
      "mutation RunCDR($id: ID!, $args: Map) { runPluginOperation(plugin_id: $id, args: $args) }",
      { id: PLUGIN_ID, args: args }
    ).then(function (data) {
      const result = data.runPluginOperation;
      if (result && typeof result === "object" && result.error) {
        throw new Error(result.error);
      }
      return result;
    });
  }

  // -- formatting ----------------------------------------------------------

  function humanBytes(value) {
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let size = Number(value) || 0;
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) {
      size /= 1024;
      unit += 1;
    }
    return unit === 0 ? size.toFixed(0) + " B" : size.toFixed(2) + " " + units[unit];
  }

  function humanDuration(seconds) {
    const total = Math.round(Number(seconds) || 0);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    const pad = function (n) { return n < 10 ? "0" + n : String(n); };
    return h > 0 ? h + ":" + pad(m) + ":" + pad(s) : m + ":" + pad(s);
  }

  function candidateKey(entry) {
    return entry.sceneId + ":" + entry.fileId;
  }

  // Policy presets. Each recomputes the plan in Python with overridden settings rather
  // than re-ranking here, so the page and the tasks always agree.
  const POLICY_PRESETS = [
    {
      id: "codec",
      label: "Keep the most efficient codec (AV1 > HEVC > H.264 > older)",
      overrides: { rankOrder: "codec,resolution,bitrate,size,age" },
    },
    {
      id: "resolution",
      label: "Keep the highest resolution",
      overrides: { rankOrder: "resolution,codec,bitrate,size" },
    },
    {
      id: "largest",
      label: "Keep the largest file",
      overrides: { rankOrder: "size,codec,resolution" },
    },
    {
      id: "smallest_hires",
      label: "Keep the smallest file at the highest resolution",
      overrides: { rankOrder: "resolution", tieBreaker: "smallest_of_highest_resolution" },
    },
    {
      id: "oldest",
      label: "Keep the oldest file",
      overrides: { rankOrder: "age,codec,resolution" },
    },
    {
      id: "newest",
      label: "Keep the newest file",
      overrides: { rankOrder: "age_desc,codec,resolution" },
    },
  ];

  // -- components ----------------------------------------------------------

  function StatusBadge(props) {
    const status = props.status || "";
    return React.createElement(
      "span",
      { className: "cdr-badge cdr-" + status.toLowerCase() },
      status
    );
  }

  function CandidateRow(props) {
    const entry = props.entry;
    const file = entry.file;
    const key = candidateKey(entry);
    const blocked = (entry.blockedBy || []).length > 0;
    const done = !!entry.outcome;
    // An entry the executor has already acted on is history: not selectable, and never
    // re-offered as pending work.
    const selectable = !entry.isKeeper && !blocked && !!entry.action && !done;
    const checked = props.selected.has(key);

    let disposition = "—";
    let rowClass = "";
    if (entry.outcome === "deleted") {
      disposition = "DELETED";
      rowClass = "cdr-done";
    } else if (entry.isKeeper) {
      disposition = "KEEP";
      rowClass = "cdr-keep";
    } else if (blocked) {
      disposition = "BLOCKED";
      rowClass = "cdr-blocked";
    } else if (entry.action) {
      disposition = checked ? "DELETE" : "kept (deselected)";
      rowClass = checked ? "cdr-remove" : "";
    }

    let detail = "";
    if (entry.outcome) {
      detail = "already " + entry.outcome;
    } else if (blocked) {
      detail = (entry.verdicts || [])
        .filter(function (v) { return !v.allowed; })
        .map(function (v) { return v.rail + ": " + v.reason; })
        .join("; ");
    } else if (entry.action) {
      detail = entry.action;
    }

    return React.createElement(
      "tr",
      { className: rowClass },
      React.createElement(
        "td",
        null,
        selectable
          ? React.createElement(Form.Check, {
              type: "checkbox",
              checked: checked,
              onChange: function () { props.onToggle(key); },
              "aria-label": "Select " + file.path + " for deletion",
            })
          : null
      ),
      React.createElement("td", { className: "cdr-disp" }, disposition),
      React.createElement("td", null, file.videoCodec || "?"),
      React.createElement("td", null, file.width + "×" + file.height),
      React.createElement("td", { className: "cdr-num" },
        file.bitRate ? Math.round(file.bitRate / 1000) : 0),
      React.createElement("td", { className: "cdr-num" }, humanBytes(file.size)),
      React.createElement("td", { className: "cdr-num" }, humanDuration(file.duration)),
      React.createElement(
        "td",
        null,
        React.createElement(
          Link,
          { to: "/scenes/" + entry.sceneId, "aria-label": "Open scene " + entry.sceneId },
          entry.sceneId
        )
      ),
      React.createElement("td", { className: "cdr-path", title: file.path }, file.path),
      React.createElement("td", { className: "cdr-detail" }, detail)
    );
  }

  function GroupCard(props) {
    const group = props.group;
    return React.createElement(
      "section",
      { className: "cdr-group" },
      React.createElement(
        "header",
        null,
        React.createElement("h4", null, "Group " + group.index),
        React.createElement(StatusBadge, { status: group.status }),
        React.createElement("span", { className: "cdr-why" }, group.reason),
        React.createElement(
          "span",
          { className: "cdr-why" },
          "deciding key: ",
          React.createElement("code", null, group.decidingKey || "—")
        )
      ),
      React.createElement(
        "div",
        {
          className: "cdr-scroll", tabIndex: 0, role: "region",
          "aria-label": "Group " + group.index + " candidate files",
        },
        React.createElement(
          "table",
          { className: "cdr-table" },
          React.createElement(
            "caption",
            { className: "cdr-sr-only" },
            "Duplicate group " + group.index + " — " + group.status + " — candidate files"
          ),
          React.createElement(
            "thead",
            null,
            React.createElement(
              "tr",
              null,
              ["Select", "Action", "Codec", "Resolution", "kb/s", "Size", "Duration",
               "Scene", "Path", "Detail"].map(function (label, i) {
                return React.createElement(
                  "th",
                  { key: i, scope: "col" },
                  i === 0
                    ? React.createElement("span", { className: "cdr-sr-only" }, label)
                    : label
                );
              })
            )
          ),
          React.createElement(
            "tbody",
            null,
            (group.candidates || []).map(function (entry) {
              return React.createElement(CandidateRow, {
                key: candidateKey(entry),
                entry: entry,
                selected: props.selected,
                onToggle: props.onToggle,
              });
            })
          )
        )
      )
    );
  }

  // -- settings panel ------------------------------------------------------
  //
  // Stash's own plugin settings only support STRING, NUMBER, and BOOLEAN, so an ordered
  // list can only be a comma-separated string there and an enum can only be free text.
  // Both are easy to get wrong in ways that change which file gets deleted, so the real
  // editors live here and write back to the same settings.
  //
  // Option lists come from the backend's `schema` payload, never hardcoded here, so
  // adding a rank key in Python cannot leave this page offering a stale set.

  // Presentation only - these describe options, they never decide policy.
  const OPTION_LABELS = {
    rankOrder: {
      codec: "Codec — by preference order below",
      resolution: "Resolution — more pixels wins",
      resolution_asc: "Resolution — fewer pixels wins",
      bitrate: "Bitrate — higher wins",
      bitrate_asc: "Bitrate — lower wins",
      size: "File size — larger wins",
      size_asc: "File size — smaller wins",
      framerate: "Frame rate — higher wins",
      duration: "Duration — longer wins",
      age: "Age — older wins",
      age_desc: "Age — newer wins",
      audio_codec: "Audio codec — by preference order",
      path_priority: "Path — earlier in Preferred Paths wins",
      organized: "Organized — an organized scene wins",
    },
    tieBreaker: {
      skip: "Skip the group and flag it for review (safest)",
      smallest_of_highest_resolution: "Keep the smallest file at the highest resolution",
      largest_of_highest_resolution: "Keep the largest file at the highest resolution",
      smallest: "Keep the smallest file",
      largest: "Keep the largest file",
      oldest: "Keep the oldest file",
      newest: "Keep the newest file",
    },
    metadataPolicy: {
      merge: "Merge — fold the losing scene's tags and counts into the keeper first",
      skip: "Skip — leave the group alone rather than lose metadata",
      ignore: "Ignore — discard the losing scene's metadata",
    },
  };

  const SETTING_LABELS = {
    rankOrder: "Rank Order",
    codecPreference: "Codec Preference",
    audioCodecPreference: "Audio Codec Preference",
    tieBreaker: "Tie Breaker",
    metadataPolicy: "Metadata Policy",
  };

  const SETTING_HELP = {
    rankOrder:
      "Highest priority first. The first key that distinguishes two files decides which " +
      "one is kept.",
    codecPreference:
      "Most preferred codec first. Use the names ffmpeg reports — HEVC is 'hevc', not " +
      "'h265'. A codec not listed here sorts last.",
    audioCodecPreference: "Only used when Audio codec appears in Rank Order.",
    tieBreaker: "Applied only when every rank key ties.",
    metadataPolicy:
      "What to do when the losing scene carries tags, performers, ratings, or markers " +
      "the keeper does not have.",
  };

  function labelFor(setting, value) {
    const map = OPTION_LABELS[setting];
    return (map && map[value]) || value;
  }

  /**
   * An ordered list the operator can rearrange.
   *
   * Drag and drop is the primary interaction, but every action is also available as a
   * real button: drag-only reordering is unusable with a keyboard or a screen reader,
   * and this list decides which video files get deleted.
   */
  function ReorderableList(props) {
    const items = props.items;
    // 0 is a legitimate minimum, so a falsy-coalescing default would silently forbid
    // emptying an optional list.
    const minItems = typeof props.minItems === "number" ? props.minItems : 1;
    const [dragIndex, setDragIndex] = React.useState(null);
    const [overIndex, setOverIndex] = React.useState(null);
    const [announcement, setAnnouncement] = React.useState("");

    function move(from, to) {
      if (to < 0 || to >= items.length || from === to) return;
      const next = items.slice();
      const [moved] = next.splice(from, 1);
      next.splice(to, 0, moved);
      props.onChange(next);
      setAnnouncement(labelFor(props.setting, moved) + " moved to position " + (to + 1) + " of " + next.length);
    }

    function remove(index) {
      const next = items.slice();
      const [dropped] = next.splice(index, 1);
      props.onChange(next);
      setAnnouncement(labelFor(props.setting, dropped) + " removed");
    }

    const remaining = (props.allowed || []).filter(function (option) {
      return items.indexOf(option) === -1;
    });

    return React.createElement(
      "div",
      { className: "cdr-reorder" },
      React.createElement(
        "ol",
        { className: "cdr-reorder-list" },
        items.map(function (item, index) {
          const classes = ["cdr-reorder-item"];
          if (dragIndex === index) classes.push("cdr-dragging");
          if (overIndex === index && dragIndex !== index) classes.push("cdr-dragover");
          const label = labelFor(props.setting, item);
          return React.createElement(
            "li",
            {
              key: item,
              className: classes.join(" "),
              draggable: true,
              onDragStart: function (e) {
                setDragIndex(index);
                // Firefox requires data to be set for a drag to begin at all.
                if (e.dataTransfer) {
                  e.dataTransfer.effectAllowed = "move";
                  e.dataTransfer.setData("text/plain", String(index));
                }
              },
              onDragOver: function (e) {
                e.preventDefault();
                setOverIndex(index);
              },
              onDragLeave: function () {
                setOverIndex(function (current) {
                  return current === index ? null : current;
                });
              },
              onDrop: function (e) {
                e.preventDefault();
                let from = dragIndex;
                if (from === null && e.dataTransfer) {
                  from = parseInt(e.dataTransfer.getData("text/plain"), 10);
                }
                if (from !== null && !isNaN(from)) move(from, index);
                setDragIndex(null);
                setOverIndex(null);
              },
              onDragEnd: function () {
                setDragIndex(null);
                setOverIndex(null);
              },
            },
            React.createElement(
              "span",
              { className: "cdr-grip", "aria-hidden": "true", title: "Drag to reorder" },
              "⠿"
            ),
            React.createElement("span", { className: "cdr-rank-num" }, index + 1),
            React.createElement("span", { className: "cdr-reorder-label" }, label),
            React.createElement(
              "span",
              { className: "cdr-reorder-actions" },
              // Deliberately never `disabled`: disabling the button the user just
              // pressed drops focus to document.body, which loses their place in the
              // form. At the boundary these no-op and say so instead.
              React.createElement(
                Button,
                {
                  variant: "secondary", size: "sm",
                  onClick: function () {
                    if (index === 0) {
                      setAnnouncement(label + " is already first");
                      return;
                    }
                    move(index, index - 1);
                  },
                  "aria-label": "Move " + label + " up",
                  title: "Move up",
                },
                "↑"
              ),
              React.createElement(
                Button,
                {
                  variant: "secondary", size: "sm",
                  onClick: function () {
                    if (index === items.length - 1) {
                      setAnnouncement(label + " is already last");
                      return;
                    }
                    move(index, index + 1);
                  },
                  "aria-label": "Move " + label + " down",
                  title: "Move down",
                },
                "↓"
              ),
              React.createElement(
                Button,
                {
                  variant: "secondary", size: "sm",
                  onClick: function () {
                    if (items.length <= minItems) {
                      setAnnouncement(
                        "At least " + minItems + " entry is required, so " + label +
                        " cannot be removed"
                      );
                      return;
                    }
                    remove(index);
                  },
                  "aria-label": "Remove " + label,
                  title: "Remove",
                },
                "×"
              )
            )
          );
        })
      ),
      // Screen readers get told what happened; drag events alone announce nothing.
      React.createElement(
        "div",
        { className: "cdr-sr-only", role: "status", "aria-live": "polite" },
        announcement
      ),
      minItems > 0 && items.length <= minItems
        ? React.createElement(
            "p",
            { className: "cdr-field-help" },
            "At least " + minItems + " entry is required, so the last one cannot be removed."
          )
        : null,
      React.createElement(AddEntry, {
        remaining: remaining,
        allowFreeText: !!props.allowFreeText,
        setting: props.setting,
        onAdd: function (value) {
          if (!value || items.indexOf(value) !== -1) return;
          props.onChange(items.concat([value]));
          setAnnouncement(labelFor(props.setting, value) + " added at position " + (items.length + 1));
        },
      })
    );
  }

  function AddEntry(props) {
    const [choice, setChoice] = React.useState("");
    const [freeText, setFreeText] = React.useState("");

    return React.createElement(
      "div",
      { className: "cdr-add-entry" },
      props.remaining.length
        ? React.createElement(
            React.Fragment,
            null,
            React.createElement(
              Form.Control,
              {
                as: "select", size: "sm", value: choice,
                "aria-label": "Entry to add",
                onChange: function (e) { setChoice(e.target.value); },
              },
              React.createElement("option", { value: "" }, "Add…"),
              props.remaining.map(function (option) {
                return React.createElement("option", { key: option, value: option },
                  labelFor(props.setting, option));
              })
            ),
            React.createElement(
              Button,
              {
                variant: "secondary", size: "sm", disabled: !choice,
                "aria-label": "Add to " + (SETTING_LABELS[props.setting] || props.setting),
                onClick: function () { props.onAdd(choice); setChoice(""); },
              },
              "Add"
            )
          )
        : null,
      props.allowFreeText
        ? React.createElement(
            React.Fragment,
            null,
            React.createElement(Form.Control, {
              size: "sm", value: freeText, placeholder: "other codec…",
              "aria-label": "Add a codec not in the list",
              onChange: function (e) { setFreeText(e.target.value); },
              onKeyDown: function (e) {
                if (e.key === "Enter") {
                  e.preventDefault();
                  props.onAdd(freeText.trim().toLowerCase());
                  setFreeText("");
                }
              },
            }),
            React.createElement(
              Button,
              {
                variant: "secondary", size: "sm", disabled: !freeText.trim(),
                "aria-label": "Add to " + (SETTING_LABELS[props.setting] || props.setting),
                onClick: function () {
                  props.onAdd(freeText.trim().toLowerCase());
                  setFreeText("");
                },
              },
              "Add"
            )
          )
        : null
    );
  }

  function EnumSelect(props) {
    return React.createElement(
      Form.Control,
      {
        as: "select",
        value: props.value,
        "aria-label": SETTING_LABELS[props.setting] || props.setting,
        onChange: function (e) { props.onChange(e.target.value); },
      },
      (props.options || []).map(function (option) {
        return React.createElement("option", { key: option, value: option },
          labelFor(props.setting, option));
      })
    );
  }

  function SettingField(props) {
    // The visible label describes a whole reorderable list, not one input, so <label>
    // would associate with nothing. A named group does the association properly and
    // ties the help text in as a description.
    const labelId = "cdr-" + props.setting + "-label";
    const helpId = "cdr-" + props.setting + "-help";
    return React.createElement(
      "div",
      {
        className: "cdr-field",
        role: "group",
        "aria-labelledby": labelId,
        "aria-describedby": SETTING_HELP[props.setting] ? helpId : undefined,
      },
      React.createElement(
        "span",
        { className: "cdr-field-label", id: labelId },
        SETTING_LABELS[props.setting] || props.setting
      ),
      SETTING_HELP[props.setting]
        ? React.createElement("p", { className: "cdr-field-help", id: helpId },
            SETTING_HELP[props.setting])
        : null,
      props.children
    );
  }

  function SettingsPanel(props) {
    const schema = props.schema || {};
    const saved = props.config || {};
    const [draft, setDraft] = React.useState(null);
    const [saving, setSaving] = React.useState(false);
    const [error, setError] = React.useState("");
    const [savedNotice, setSavedNotice] = React.useState("");

    // Re-seed the draft whenever the saved settings change, so an external edit in
    // Stash's own settings page is not silently overwritten by a stale draft.
    React.useEffect(
      function () {
        setDraft({
          rankOrder: (saved.rankOrder || []).slice(),
          codecPreference: (saved.codecPreference || []).slice(),
          audioCodecPreference: (saved.audioCodecPreference || []).slice(),
          tieBreaker: saved.tieBreaker || "skip",
          metadataPolicy: saved.metadataPolicy || "merge",
        });
        setError("");
      },
      [
        (saved.rankOrder || []).join(","),
        (saved.codecPreference || []).join(","),
        (saved.audioCodecPreference || []).join(","),
        saved.tieBreaker,
        saved.metadataPolicy,
      ]
    );

    if (!draft) return null;

    function set(key, value) {
      setDraft(function (prev) {
        const next = Object.assign({}, prev);
        next[key] = value;
        return next;
      });
      setSavedNotice("");
    }

    const dirty =
      draft.rankOrder.join(",") !== (saved.rankOrder || []).join(",") ||
      draft.codecPreference.join(",") !== (saved.codecPreference || []).join(",") ||
      draft.audioCodecPreference.join(",") !==
        (saved.audioCodecPreference || []).join(",") ||
      draft.tieBreaker !== saved.tieBreaker ||
      draft.metadataPolicy !== saved.metadataPolicy;

    function save() {
      setSaving(true);
      setError("");
      setSavedNotice("");
      // Sent as comma-separated strings because that is what Stash stores; the backend
      // validates and rejects anything malformed rather than falling back to a default.
      runOperation({
        mode: "ui_save_settings",
        settings: {
          rankOrder: draft.rankOrder.join(","),
          codecPreference: draft.codecPreference.join(","),
          audioCodecPreference: draft.audioCodecPreference.join(","),
          tieBreaker: draft.tieBreaker,
          metadataPolicy: draft.metadataPolicy,
        },
      })
        .then(function () {
          setSavedNotice("Settings saved.");
          return props.onSaved();
        })
        .catch(function (err) { setError(String(err.message || err)); })
        .then(function () { setSaving(false); });
    }

    function resetToDefaults() {
      const defaults = schema.defaults || {};
      const split = function (value) {
        return String(value || "").split(",").map(function (s) { return s.trim(); })
          .filter(Boolean);
      };
      setDraft({
        rankOrder: split(defaults.rankOrder),
        codecPreference: split(defaults.codecPreference),
        audioCodecPreference: split(defaults.audioCodecPreference),
        tieBreaker: defaults.tieBreaker || "skip",
        metadataPolicy: defaults.metadataPolicy || "merge",
      });
      setSavedNotice("");
    }

    const enums = schema.enumSettings || {};

    return React.createElement(
      "section",
      { className: "cdr-settings" },
      React.createElement("h4", null, "Ranking policy"),
      React.createElement(
        "p",
        { className: "cdr-sub" },
        "These are the same settings Stash shows under Plugins, edited here because an ",
        "ordered list and a fixed set of choices cannot be expressed as free text ",
        "without inviting mistakes. Saving writes back to the plugin settings."
      ),

      error ? React.createElement(Alert, { variant: "danger" }, error) : null,
      savedNotice ? React.createElement(Alert, { variant: "success" }, savedNotice) : null,

      React.createElement(
        "div",
        { className: "cdr-settings-grid" },
        React.createElement(
          SettingField,
          { setting: "rankOrder" },
          React.createElement(ReorderableList, {
            setting: "rankOrder",
            items: draft.rankOrder,
            allowed: schema.validRankKeys || [],
            minItems: 1,
            onChange: function (next) { set("rankOrder", next); },
          })
        ),
        React.createElement(
          SettingField,
          { setting: "codecPreference" },
          React.createElement(ReorderableList, {
            setting: "codecPreference",
            items: draft.codecPreference,
            allowed: schema.knownCodecs || [],
            allowFreeText: true,
            minItems: 1,
            onChange: function (next) { set("codecPreference", next); },
          })
        ),
        React.createElement(
          SettingField,
          { setting: "audioCodecPreference" },
          React.createElement(ReorderableList, {
            setting: "audioCodecPreference",
            items: draft.audioCodecPreference,
            allowed: schema.knownAudioCodecs || [],
            allowFreeText: true,
            minItems: 0,
            onChange: function (next) { set("audioCodecPreference", next); },
          })
        ),
        React.createElement(
          SettingField,
          { setting: "tieBreaker" },
          React.createElement(EnumSelect, {
            setting: "tieBreaker",
            value: draft.tieBreaker,
            options: enums.tieBreaker || schema.validTieBreakers || [],
            onChange: function (value) { set("tieBreaker", value); },
          })
        ),
        React.createElement(
          SettingField,
          { setting: "metadataPolicy" },
          React.createElement(EnumSelect, {
            setting: "metadataPolicy",
            value: draft.metadataPolicy,
            options: enums.metadataPolicy || schema.validMetadataPolicies || [],
            onChange: function (value) { set("metadataPolicy", value); },
          })
        )
      ),

      React.createElement(
        "div",
        { className: "cdr-settings-actions" },
        React.createElement(
          Button,
          { variant: "primary", disabled: saving || !dirty, onClick: save },
          saving ? "Saving…" : "Save settings"
        ),
        React.createElement(
          Button,
          { variant: "secondary", disabled: saving, onClick: resetToDefaults },
          "Reset to defaults"
        ),
        dirty
          ? React.createElement("span", { className: "cdr-count cdr-muted" },
              "Unsaved changes. The current plan was built with the saved values.")
          : null
      )
    );
  }

  function DuplicateRemoverPage() {
    const [plan, setPlan] = React.useState(null);
    // Live plugin settings, re-read from the backend. Deliberately NOT taken from
    // plan.summary.config, which is a snapshot of the settings at the moment the plan
    // was written — toggling confirmDestructive afterwards would never be noticed.
    const [liveConfig, setLiveConfig] = React.useState(null);
    const [schema, setSchema] = React.useState(null);
    const [showSettings, setShowSettings] = React.useState(false);
    const [configError, setConfigError] = React.useState("");
    const [selected, setSelected] = React.useState(new Set());
    const [busy, setBusy] = React.useState("");
    const [error, setError] = React.useState("");
    const [notice, setNotice] = React.useState("");
    const [preset, setPreset] = React.useState("codec");
    const [distance, setDistance] = React.useState(0);
    const [durationDiff, setDurationDiff] = React.useState(1);
    const [confirming, setConfirming] = React.useState(false);
    const cancelRef = React.useRef(null);

    // Focus the safe option, never "Yes, delete": showing the prompt unmounts the button
    // that opened it, which otherwise drops focus to document.body with no announcement.
    React.useEffect(function () {
      if (confirming && cancelRef.current) cancelRef.current.focus();
    }, [confirming]);

    // Everything the plan proposes deleting starts checked, mirroring the report.
    const applyPlan = React.useCallback(function (payload) {
      setPlan(payload);
      const next = new Set();
      (payload.groups || []).forEach(function (group) {
        (group.candidates || []).forEach(function (entry) {
          if (!entry.isKeeper && entry.action && !(entry.blockedBy || []).length
              && !entry.outcome) {
            next.add(candidateKey(entry));
          }
        });
      });
      setSelected(next);
      setConfirming(false);
    }, []);

    const loadConfig = React.useCallback(function () {
      return runOperation({ mode: "ui_config" })
        .then(function (payload) {
          setLiveConfig((payload && payload.config) || null);
          setSchema((payload && payload.schema) || null);
          setConfigError("");
        })
        .catch(function (err) {
          // Fail closed: without knowing the setting, deletion stays disabled.
          setLiveConfig(null);
          setConfigError(String(err.message || err));
        });
    }, []);

    const loadLast = React.useCallback(function () {
      setBusy("Loading the last plan…");
      setError("");
      setNotice("");
      runOperation({ mode: "ui_load" })
        .then(applyPlan)
        .catch(function (err) { setError(String(err.message || err)); })
        .then(function () { setBusy(""); });
    }, [applyPlan]);

    const recompute = React.useCallback(function () {
      const chosen = POLICY_PRESETS.filter(function (p) { return p.id === preset; })[0];
      const overrides = Object.assign({}, chosen ? chosen.overrides : {}, {
        phashDistance: Number(distance),
        durationDiff: Number(durationDiff),
      });
      setBusy("Scanning for duplicates. On a large library this takes a while…");
      setError("");
      setNotice("");
      runOperation({ mode: "ui_plan", overrides: overrides })
        .then(function (payload) {
          applyPlan(payload);
          setNotice("Plan rebuilt. Nothing has been deleted.");
          return loadConfig();
        })
        .catch(function (err) { setError(String(err.message || err)); })
        .then(function () { setBusy(""); });
    }, [applyPlan, loadConfig, preset, distance, durationDiff]);

    React.useEffect(function () {
      loadConfig();
      loadLast();
    }, [loadConfig, loadLast]);

    const toggle = React.useCallback(function (key) {
      setSelected(function (prev) {
        const next = new Set(prev);
        if (next.has(key)) next.delete(key);
        else next.add(key);
        return next;
      });
      setConfirming(false);
    }, []);

    // Selection actions only narrow what the plan already approved. Choosing a
    // different keeper is a policy change, so it goes through recompute instead.
    function eligible() {
      const out = [];
      ((plan && plan.groups) || []).forEach(function (group) {
        (group.candidates || []).forEach(function (entry) {
          if (!entry.isKeeper && entry.action && !(entry.blockedBy || []).length
              && !entry.outcome) {
            out.push({ group: group, entry: entry });
          }
        });
      });
      return out;
    }

    function selectNone() {
      setSelected(new Set());
      setConfirming(false);
    }

    function selectAll() {
      setSelected(new Set(eligible().map(function (x) { return candidateKey(x.entry); })));
      setConfirming(false);
    }

    function selectWhereKeeperIs(codec) {
      const next = new Set();
      ((plan && plan.groups) || []).forEach(function (group) {
        const keeper = (group.candidates || []).filter(function (c) { return c.isKeeper; })[0];
        if (!keeper) return;
        const keeperCodec = (keeper.file.videoCodec || "").toLowerCase();
        if (keeperCodec !== codec) return;
        (group.candidates || []).forEach(function (entry) {
          if (!entry.isKeeper && entry.action && !(entry.blockedBy || []).length
              && !entry.outcome) {
            next.add(candidateKey(entry));
          }
        });
      });
      setSelected(next);
      setConfirming(false);
    }

    function selectNonMatchingCodec() {
      // Parity with the built-in checker's "only select matching codecs" safety, in
      // reverse: only groups whose files disagree on codec, which is exactly the
      // post-transcode case.
      const next = new Set();
      ((plan && plan.groups) || []).forEach(function (group) {
        const codecs = {};
        (group.candidates || []).forEach(function (c) {
          codecs[(c.file.videoCodec || "").toLowerCase()] = true;
        });
        if (Object.keys(codecs).length < 2) return;
        (group.candidates || []).forEach(function (entry) {
          if (!entry.isKeeper && entry.action && !(entry.blockedBy || []).length
              && !entry.outcome) {
            next.add(candidateKey(entry));
          }
        });
      });
      setSelected(next);
      setConfirming(false);
    }

    function runDelete() {
      setBusy("Deleting. Guard rails are being re-checked against live state…");
      setError("");
      setNotice("");
      runOperation({ mode: "ui_execute", selection: Array.from(selected) })
        .then(function (result) {
          setNotice(
            "Deleted " + (result.filesDeleted || 0) + " files, destroyed " +
            (result.scenesDestroyed || 0) + " scenes, reclaimed " +
            humanBytes(result.bytesReclaimed || 0) + ". See audit.jsonl for the record."
          );
          return runOperation({ mode: "ui_load" })
            .then(applyPlan)
            .then(loadConfig)
            .catch(function (err) {
              setError(
                "The deletion succeeded but the plan could not be reloaded, so the list " +
                "below is stale: " + String(err.message || err)
              );
            });
        })
        .catch(function (err) { setError(String(err.message || err)); })
        .then(function () {
          setBusy("");
          setConfirming(false);
        });
    }

    const summary = (plan && plan.summary) || {};
    const selectedBytes = eligible().reduce(function (total, x) {
      return selected.has(candidateKey(x.entry)) ? total + (x.entry.file.size || 0) : total;
    }, 0);
    // The destructive gate comes from live settings only.
    const confirmEnabled = !!(liveConfig && liveConfig.confirmDestructive);

    return React.createElement(
      "div",
      { className: "container-fluid cdr-page" },
      React.createElement("h3", null, "Customizable Duplicate Remover"),
      React.createElement(
        "p",
        { className: "cdr-sub" },
        "Ranking and guard rails run in the plugin backend. Selecting a row here only ",
        "narrows what the plan already approved — to keep a different file, change the ",
        "policy and rebuild."
      ),

      error
        ? React.createElement(Alert, { variant: "danger" }, error)
        : null,
      notice
        ? React.createElement(Alert, { variant: "success" }, notice)
        : null,
      configError
        ? React.createElement(
            Alert,
            { variant: "warning" },
            "Could not read the plugin settings, so deleting stays disabled: " + configError
          )
        : null,
      liveConfig && !confirmEnabled
        ? React.createElement(
            Alert,
            { variant: "warning" },
            "Confirm Destructive Operations is off in the plugin settings, so deleting ",
            "will refuse. Turn it on once this list looks right, then ",
            React.createElement(
              Button,
              { variant: "link", size: "sm", className: "p-0 align-baseline",
                onClick: loadConfig },
              "re-check settings"
            ),
            "."
          )
        : null,

      React.createElement(
        "div",
        { className: "cdr-controls" },
        React.createElement(
          Form.Group,
          { className: "cdr-control", controlId: "cdr-preset" },
          React.createElement(Form.Label, null, "Keep which file"),
          React.createElement(
            Form.Control,
            {
              as: "select",
              value: preset,
              onChange: function (e) { setPreset(e.target.value); },
            },
            POLICY_PRESETS.map(function (p) {
              return React.createElement("option", { key: p.id, value: p.id }, p.label);
            })
          )
        ),
        React.createElement(
          Form.Group,
          { className: "cdr-control cdr-narrow", controlId: "cdr-phash-distance" },
          React.createElement(Form.Label, null, "Phash distance"),
          React.createElement(Form.Control, {
            type: "number", min: 0, max: 64, value: distance,
            onChange: function (e) { setDistance(e.target.value); },
          })
        ),
        React.createElement(
          Form.Group,
          { className: "cdr-control cdr-narrow", controlId: "cdr-duration-diff" },
          React.createElement(Form.Label, null, "Duration diff (s)"),
          React.createElement(Form.Control, {
            type: "number", min: 0, step: "0.5", value: durationDiff,
            onChange: function (e) { setDurationDiff(e.target.value); },
          })
        ),
        React.createElement(
          "div",
          { className: "cdr-control cdr-actions" },
          React.createElement(
            Button,
            { variant: "primary", disabled: !!busy, onClick: recompute },
            "Rebuild plan"
          ),
          React.createElement(
            Button,
            { variant: "secondary", disabled: !!busy, onClick: loadLast },
            "Reload last plan"
          ),
          React.createElement(
            Button,
            {
              variant: showSettings ? "primary" : "secondary",
              onClick: function () { setShowSettings(!showSettings); },
              "aria-expanded": showSettings,
            },
            showSettings ? "Hide policy settings" : "Edit policy settings"
          )
        )
      ),

      showSettings && liveConfig
        ? React.createElement(SettingsPanel, {
            config: liveConfig,
            schema: schema,
            onSaved: loadConfig,
          })
        : null,

      busy
        ? React.createElement(
            "div",
            { className: "cdr-busy", role: "status", "aria-live": "polite" },
            React.createElement(Spinner, { animation: "border", size: "sm" }),
            " ",
            busy
          )
        : null,

      plan
        ? React.createElement(
            "div",
            { className: "cdr-toolbar" },
            React.createElement(
              Dropdown,
              null,
              React.createElement(Dropdown.Toggle, { variant: "secondary", disabled: !!busy },
                "Select…"),
              React.createElement(
                Dropdown.Menu,
                null,
                React.createElement(Dropdown.Item, { onClick: selectNone }, "Select none"),
                React.createElement(Dropdown.Item, { onClick: selectAll },
                  "Select every file the plan proposes deleting"),
                React.createElement(Dropdown.Divider, null),
                React.createElement(Dropdown.Item, { onClick: selectNonMatchingCodec },
                  "Only groups whose files differ in codec"),
                React.createElement(Dropdown.Item,
                  { onClick: function () { selectWhereKeeperIs("hevc"); } },
                  "Only groups where the kept file is HEVC"),
                React.createElement(Dropdown.Item,
                  { onClick: function () { selectWhereKeeperIs("av1"); } },
                  "Only groups where the kept file is AV1")
              )
            ),
            React.createElement(
              "span",
              { className: "cdr-count", role: "status", "aria-live": "polite",
                "aria-atomic": "true" },
              selected.size + " selected · " + humanBytes(selectedBytes) + " reclaimable"
            ),
            React.createElement(
              "span",
              { className: "cdr-count cdr-muted" },
              (summary.groups || 0) + " groups · " +
              ((summary.groupsByStatus && summary.groupsByStatus.AMBIGUOUS) || 0) +
              " ambiguous · " +
              ((summary.groupsByStatus && summary.groupsByStatus.PROTECTED) || 0) +
              " protected"
            ),
            confirming
              ? React.createElement(
                  "span",
                  {
                    className: "cdr-confirm",
                    role: "alert",
                    onKeyDown: function (e) {
                      if (e.key === "Escape") setConfirming(false);
                    },
                  },
                  React.createElement(
                    "strong",
                    null,
                    "Permanently delete " + selected.size + " files from disk?"
                  ),
                  React.createElement(
                    Button,
                    { variant: "danger", size: "sm", onClick: runDelete },
                    "Yes, delete"
                  ),
                  React.createElement(
                    Button,
                    {
                      variant: "secondary", size: "sm", ref: cancelRef,
                      onClick: function () { setConfirming(false); },
                    },
                    "Cancel"
                  )
                )
              : React.createElement(
                  Button,
                  {
                    variant: "danger",
                    disabled: !!busy || selected.size === 0 || !confirmEnabled,
                    onClick: function () { setConfirming(true); },
                  },
                  "Delete selected"
                )
          )
        : null,

      plan
        ? (plan.groups || []).map(function (group) {
            return React.createElement(GroupCard, {
              key: group.index,
              group: group,
              selected: selected,
              onToggle: toggle,
            });
          })
        : null,

      plan && !(plan.groups || []).length && !busy
        ? React.createElement(
            Alert,
            { variant: "info" },
            "No duplicate groups in the last plan. Rebuild to scan again."
          )
        : null
    );
  }

  PluginApi.register.route(ROUTE, DuplicateRemoverPage);

  // A link on Settings > Tools, next to the built-in Scene Duplicate Checker.
  PluginApi.patch.before("SettingsToolsSection", function (props) {
    const { Setting } = PluginApi.components;
    return [
      {
        children: React.createElement(
          React.Fragment,
          null,
          props.children,
          React.createElement(Setting, {
            heading: React.createElement(
              Link,
              { to: ROUTE },
              React.createElement(Button, null, "Customizable Duplicate Remover")
            ),
            subHeading:
              "Resolve phash duplicates by codec, resolution, bitrate, or a custom " +
              "ranking, then delete after review.",
          })
        ),
      },
    ];
  });

  // And a main-nav entry, since this is a page you return to.
  PluginApi.patch.before("MainNavBar.MenuItems", function (props) {
    return [
      {
        children: React.createElement(
          React.Fragment,
          null,
          props.children,
          React.createElement(
            NavLink,
            { className: "nav-link", to: ROUTE },
            React.createElement(
              Button,
              { className: "minimal p-4 p-xl-2 d-flex d-xl-inline-block", title: "Duplicates" },
              "Dupes"
            )
          )
        ),
      },
    ];
  });
})();
