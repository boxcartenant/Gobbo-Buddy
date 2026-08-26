// ============================================================
// GOBBOBRIDGE
// GobboNet MOD -> Python
//
// Architecture:
//
//     Python
//       |
//       | GET /command
//       v
//     GobboNet JS
//       |
//       | sendMessage()
//       v
//     GobboNet conversation
//       |
//       | response
//       v
//     GobboNet JS
//       |
//       | POST /result
//       v
//     Python
//
// GobboNet itself owns the conversation/thread/character/lore.
// Python only supplies user messages and receives responses.
// ============================================================


const GOBBO_BRIDGE_URL =
    "http://127.0.0.1:8765";


console.log(
    "===================================================="
);

console.log(
    "[GobboBridge] LOADED"
);

console.log(
    "[GobboBridge] Bridge:",
    GOBBO_BRIDGE_URL
);

console.log(
    "===================================================="
);


// ============================================================
// STATE
// ============================================================

let gobboBridgeBusy = false;


// ============================================================
// COMMAND POLLER
// ============================================================

async function gobboBridgePoll() {

    // Don't accept another Python command while GobboNet
    // is still processing the previous one.
    if (gobboBridgeBusy) {
        return;
    }


    try {

        const response = await fetch(
            `${GOBBO_BRIDGE_URL}/command?t=${Date.now()}`,
            {
                method: "GET",
                cache: "no-store"
            }
        );


        if (!response.ok) {

            console.warn(
                "[GobboBridge] /command HTTP error:",
                response.status
            );

            return;
        }


        const command =
            await response.json();


        // Empty object means there is currently no work.
        if (
            !command ||
            !command.id
        ) {

            return;
        }


        console.log(
            "===================================================="
        );

        console.log(
            "[GobboBridge] COMMAND RECEIVED:",
            command
        );


        gobboBridgeBusy = true;


        try {

            await gobboBridgeExecute(
                command
            );

        } catch (error) {

            console.error(
                "[GobboBridge] EXECUTION FAILED:",
                error
            );


            // Try to tell Python that the command failed.
            try {

                await gobboBridgeSendResult({

                    id: command.id,

                    ok: false,

                    error: String(error),

                    stack:
                        error &&
                        error.stack
                            ? error.stack
                            : ""
                });

            } catch (resultError) {

                // Python may have gone away. That's okay.
                console.warn(
                    "[GobboBridge] Could not report error to Python:",
                    resultError
                );
            }

        } finally {

            gobboBridgeBusy = false;
        }


    } catch (error) {

        // Python is probably not running.
        //
        // Do NOT spam the console every 500 ms.
        //
        // The polling loop will continue and automatically
        // reconnect when Python comes back.
    }
}


// ============================================================
// EXECUTE COMMAND
// ============================================================

async function gobboBridgeExecute(command) {

    console.log(
        "[GobboBridge] EXECUTING COMMAND:",
        command
    );


    // --------------------------------------------------------
    // Get GobboNet's currently active conversation.
    //
    // IMPORTANT:
    //
    // We intentionally do NOT use command.thread_id here.
    //
    // GobboNet owns the active conversation. This allows its
    // normal character cards, lore, summaries, etc. to remain
    // attached to the conversation.
    // --------------------------------------------------------

    if (
        typeof getActiveThread !== "function"
    ) {

        throw new Error(
            "GobboNet getActiveThread() is not available"
        );
    }


    const activeThread =
        getActiveThread();


    if (!activeThread) {

        throw new Error(
            "GobboNet has no active conversation"
        );
    }


    console.log(
        "[GobboBridge] ACTIVE GOBBONET THREAD:",
        activeThread
    );


    console.log(
        "[GobboBridge] THREAD ID:",
        activeThread.id
    );


    console.log(
        "[GobboBridge] THREAD NAME:",
        activeThread.name
    );


    console.log(
        "[GobboBridge] EXISTING MESSAGE COUNT:",
        Array.isArray(activeThread.messages)
            ? activeThread.messages.length
            : "unknown"
    );


    // --------------------------------------------------------
    // Verify GobboNet's normal sendMessage() function.
    // --------------------------------------------------------

    if (
        typeof sendMessage !== "function"
    ) {

        throw new Error(
            "GobboNet sendMessage() is not available"
        );
    }


    // --------------------------------------------------------
    // Send the user's message through GobboNet.
    //
    // This is the important bit:
    //
    // We are NOT manually constructing an LLM request.
    //
    // We are using GobboNet's own message pipeline.
    // --------------------------------------------------------

    console.log(
        "[GobboBridge] Sending message through GobboNet..."
    );


    const sendResult =
        sendMessage(
            command.message
        );


    console.log(
        "[GobboBridge] sendMessage() returned:",
        sendResult
    );


    // sendMessage() returns a Promise in GobboNet.
    if (
        sendResult &&
        typeof sendResult.then === "function"
    ) {

        await sendResult;

        console.log(
            "[GobboBridge] sendMessage() completed."
        );
    }


    // --------------------------------------------------------
    // Wait for GobboNet's assistant response.
    // --------------------------------------------------------

    console.log(
        "[GobboBridge] Waiting for assistant response..."
    );


    const assistant =
        await gobboBridgeWaitForAssistant(
            120000
        );


    const content =
        gobboBridgeExtractContent(
            assistant
        );


    console.log(
        "[GobboBridge] ASSISTANT RESPONSE:",
        content
    );


    // --------------------------------------------------------
    // Re-read the active thread.
    //
    // This lets us report the actual GobboNet thread rather
    // than Python's old fictional thread ID.
    // --------------------------------------------------------

    const finalThread =
        getActiveThread();


    console.log(
        "[GobboBridge] FINAL THREAD ID:",
        finalThread.id
    );


    console.log(
        "[GobboBridge] FINAL THREAD NAME:",
        finalThread.name
    );


    console.log(
        "[GobboBridge] FINAL MESSAGE COUNT:",
        Array.isArray(finalThread.messages)
            ? finalThread.messages.length
            : "unknown"
    );


    // --------------------------------------------------------
    // Return the response to Python.
    // --------------------------------------------------------

    await gobboBridgeSendResult({

        id: command.id,

        ok: true,

        thread_id: finalThread.id,

        thread_name: finalThread.name,

        content: content
    });


    console.log(
        "[GobboBridge] Result delivered to Python."
    );


    console.log(
        "===================================================="
    );
}


// ============================================================
// WAIT FOR ASSISTANT RESPONSE
// ============================================================

async function gobboBridgeWaitForAssistant(
    timeoutMs
) {

    const startTime =
        Date.now();


    while (
        Date.now() - startTime < timeoutMs
    ) {

        try {

            const thread =
                getActiveThread();


            if (
                thread &&
                Array.isArray(thread.messages)
            ) {

                // Search backwards because the newest message
                // is the one we care about.
                for (
                    let i =
                        thread.messages.length - 1;
                    i >= 0;
                    i--
                ) {

                    const message =
                        thread.messages[i];


                    if (
                        message &&
                        message.role === "assistant"
                    ) {

                        const content =
                            gobboBridgeExtractContent(
                                message
                            );


                        if (
                            content &&
                            content.trim()
                        ) {

                            console.log(
                                "[GobboBridge] Found assistant message:",
                                message
                            );


                            return message;
                        }
                    }
                }
            }


        } catch (error) {

            console.warn(
                "[GobboBridge] Error inspecting response:",
                error
            );
        }


        await new Promise(
            function(resolve) {

                setTimeout(
                    resolve,
                    100
                );
            }
        );
    }


    throw new Error(
        "Timed out waiting for GobboNet assistant response"
    );
}


// ============================================================
// EXTRACT MESSAGE CONTENT
// ============================================================

function gobboBridgeExtractContent(
    message
) {

    if (!message) {
        return "";
    }


    // Plain string.
    if (
        typeof message === "string"
    ) {

        return message;
    }


    // Normal GobboNet message.
    if (
        typeof message.content === "string"
    ) {

        return message.content;
    }


    // Possible structured content.
    if (
        Array.isArray(message.content)
    ) {

        return message.content
            .map(
                function(part) {

                    if (
                        typeof part === "string"
                    ) {

                        return part;
                    }


                    if (
                        part &&
                        typeof part.text === "string"
                    ) {

                        return part.text;
                    }


                    return "";
                }
            )
            .join("");
    }


    return "";
}


// ============================================================
// SEND RESULT TO PYTHON
// ============================================================

async function gobboBridgeSendResult(
    result
) {

    console.log(
        "[GobboBridge] POST /result:",
        result
    );


    const response =
        await fetch(
            `${GOBBO_BRIDGE_URL}/result`,
            {
                method: "POST",

                headers: {
                    "Content-Type":
                        "application/json"
                },

                body:
                    JSON.stringify(
                        result
                    ),

                cache: "no-store"
            }
        );


    console.log(
        "[GobboBridge] /result HTTP:",
        response.status
    );


    if (!response.ok) {

        throw new Error(
            "Python /result returned HTTP " +
            response.status
        );
    }
}


// ============================================================
// START BRIDGE
// ============================================================

console.log(
    "[GobboBridge] Starting command polling..."
);


gobboBridgePoll();


setInterval(
    gobboBridgePoll,
    500
);


console.log(
    "[GobboBridge] INITIALIZATION COMPLETE"
);