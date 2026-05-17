// build.gradle.kts dependencies:
// implementation("com.aallam.openai:openai-client:4.0.1")
// implementation("io.ktor:ktor-client-cio:3.0.0")
import com.aallam.openai.api.chat.ChatCompletionRequest
import com.aallam.openai.api.chat.ChatMessage
import com.aallam.openai.api.chat.ChatRole
import com.aallam.openai.api.model.ModelId
import com.aallam.openai.client.OpenAI
import com.aallam.openai.client.OpenAIHost
import kotlinx.coroutines.runBlocking

fun main() = runBlocking {
    val openai = OpenAI(
        token = "a2a_your_key_here",
        host = OpenAIHost(baseUrl = "https://oya.ai/api/v1/")
    )
    val completion = openai.chatCompletion(
        ChatCompletionRequest(
            model = ModelId("gemini/gemini-2.0-flash"),
            messages = listOf(ChatMessage(role = ChatRole.User, content = "Hello"))
        )
    )
    println(completion.choices.first().message.messageContent)
}
